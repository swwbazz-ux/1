import hashlib
import inspect
import json
from dataclasses import FrozenInstanceError
from datetime import date
from importlib import import_module
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models
from django.test import TestCase
from django.utils import timezone

from users.models import Employee, EmployeeAccess, Role, WorkSchedule

from .brigade_phase_calendar import (
    ERROR_ACCESS_BLOCKED,
    ERROR_ACCESS_INACTIVE,
    ERROR_ACCESS_NOT_FOUND,
    ERROR_ACCESS_WRONG_ROLE,
    ERROR_BRIGADE_NOT_FOUND,
    ERROR_CONFIRMED_VERSION_INCONSISTENT,
    ERROR_CONFIRMED_VERSION_NOT_FOUND,
    ERROR_EMPLOYEE_INACTIVE,
    ERROR_GRAPH_INCOMPLETE,
    ERROR_GRAPH_INCONSISTENT,
    ERROR_INCONSISTENT_GRAPH,
    ERROR_INVALID_BRIGADE_SET,
    ERROR_INVALID_SOURCE,
    ERROR_POLICY_MISMATCH,
    ERROR_POLICY_NOT_DEFINED,
    ERROR_SCHEDULE_DESIGNATION_MISMATCH,
    ERROR_SOURCE_FINGERPRINT_INVALID,
    ERROR_SOURCE_INVALID,
    ERROR_SOURCE_NOT_EFFECTIVE,
    ERROR_VERSION_NOT_FOUND,
    ERROR_VERSION_STALE,
    ERROR_WATCH_PERIOD_NOT_FOUND,
    ERROR_WORK_SCHEDULE_INACTIVE,
    ERROR_WORK_SCHEDULE_NOT_FOUND,
    WORK_SCHEDULE_CODE_11,
    WORK_SCHEDULE_CODE_12,
    BrigadePhaseCalendarError,
    ConfirmedBrigadePhase,
    _normalize_brigade_phases,
    confirm_watch_period_brigade_phase_version,
    create_watch_period_brigade_phase_draft,
    resolve_confirmed_brigade_phase,
)
from .models import (
    WatchPeriod,
    WatchPeriodBrigadePhaseRow,
    WatchPeriodBrigadePhaseVersion,
)


class BrigadePhaseCalendarDraftServiceTests(TestCase):
    order_sha = 'A' * 64
    schedule_sha = 'B' * 64

    def setUp(self):
        self.timekeeper_role = Role.objects.get(code='timekeeper')
        self.admin_role, _created = Role.objects.get_or_create(
            code='admin',
            defaults={'name': 'Системный администратор', 'is_active': True},
        )
        self.other_role = Role.objects.create(
            code='brigade-phase-other-role',
            name='Другая роль теста календаря фаз',
        )
        self.employee = Employee.objects.create(
            full_name='Табельщик теста календаря фаз',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.timekeeper_role,
            access_code='BPC-SERVICE-ACCESS',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.admin_employee = Employee.objects.create(
            full_name='Администратор теста календаря фаз',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.admin_employee,
            role=self.admin_role,
            access_code='BPC-SERVICE-ADMIN',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.schedule = WorkSchedule.objects.create(
            code='brigade-phase-service-schedule',
            name='График service test',
            brigade_count=4,
            is_active=True,
        )
        self.period = WatchPeriod.objects.create(
            name='Период service test календаря фаз',
            starts_on=date(2031, 2, 1),
            ends_on=date(2031, 3, 15),
        )

    def _kwargs(self, **overrides):
        values = {
            'watch_period_id': self.period.pk,
            'work_schedule_id': self.schedule.pk,
            'actor_access_id': self.access.pk,
            'order_number': '  ПРИКАЗ   № 17  ',
            'order_date': '2031-01-10',
            'effective_from': date(2031, 2, 1),
            'order_document_sha256': f'  {self.order_sha}  ',
            'schedule_designation': '  График   № 12/1  ',
            'schedule_document_sha256': self.schedule_sha,
            'brigade_phases': [
                {'brigade_number': 4, 'phase': ' DAY '},
                {'brigade_number': 2, 'phase': 'day'},
                {'brigade_number': 1, 'phase': 'night'},
                {'brigade_number': 3, 'phase': 'OFF'},
            ],
        }
        values.update(overrides)
        return values

    @staticmethod
    def _insert(model_class, instances):
        models.QuerySet.bulk_create(model_class._base_manager.all(), instances)
        return instances

    def _assert_error(self, code, **overrides):
        with self.assertRaises(BrigadePhaseCalendarError) as caught:
            create_watch_period_brigade_phase_draft(**self._kwargs(**overrides))
        self.assertEqual(caught.exception.code, code)

    def test_success_builds_complete_immutable_server_draft(self):
        before = timezone.now()
        version = create_watch_period_brigade_phase_draft(**self._kwargs())
        after = timezone.now()
        version.refresh_from_db()

        expected_snapshot = {
            'source_kind': 'official_schedule_order',
            'order': {
                'number': 'ПРИКАЗ № 17',
                'date': '2031-01-10',
                'effective_from': '2031-02-01',
                'document_sha256': self.order_sha.lower(),
            },
            'schedule': {
                'designation': 'График № 12/1',
                'document_sha256': self.schedule_sha.lower(),
            },
        }
        canonical = json.dumps(
            expected_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        self.assertEqual(version.watch_period_id, self.period.pk)
        self.assertEqual(version.work_schedule_id, self.schedule.pk)
        self.assertEqual(version.created_by_access_id, self.access.pk)
        self.assertEqual(version.status, WatchPeriodBrigadePhaseVersion.Status.DRAFT)
        self.assertEqual(version.version_number, 1)
        self.assertIsNone(version.based_on_version_id)
        self.assertIsNone(version.confirmed_by_access_id)
        self.assertIsNone(version.superseded_by_access_id)
        self.assertIsNone(version.confirmed_at)
        self.assertIsNone(version.superseded_at)
        self.assertGreaterEqual(version.created_at, before)
        self.assertLessEqual(version.created_at, after)
        self.assertEqual(version.source_snapshot, expected_snapshot)
        self.assertEqual(
            version.source_fingerprint,
            hashlib.sha256(canonical).hexdigest(),
        )
        self.assertEqual(
            list(
                version.rows.order_by('brigade_number').values_list(
                    'brigade_number',
                    'phase',
                )
            ),
            [(1, 'night'), (2, 'day'), (3, 'off'), (4, 'day')],
        )

    def test_exact_admin_access_creates_draft_with_actual_audit_actor(self):
        version = create_watch_period_brigade_phase_draft(**self._kwargs(
            actor_access_id=self.admin_access.pk,
        ))

        version.refresh_from_db()
        self.assertEqual(version.created_by_access_id, self.admin_access.pk)
        self.assertEqual(version.created_by_access.role.code, 'admin')

    def test_command_does_not_accept_server_derived_fields(self):
        forbidden = {
            'status',
            'version_number',
            'based_on_version',
            'created_at',
            'confirmed_at',
            'superseded_at',
            'confirmed_by_access',
            'superseded_by_access',
            'source_snapshot',
            'source_fingerprint',
        }
        self.assertTrue(
            forbidden.isdisjoint(
                inspect.signature(create_watch_period_brigade_phase_draft).parameters
            )
        )
        with self.assertRaises(TypeError):
            create_watch_period_brigade_phase_draft(
                **self._kwargs(),
                status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
            )

    def test_exact_access_failures_are_controlled_without_fallback(self):
        self._assert_error(ERROR_ACCESS_NOT_FOUND, actor_access_id=self.access.pk + 10000)

        self.access.role = self.other_role
        self.access.save(update_fields=['role'])
        self._assert_error(ERROR_ACCESS_WRONG_ROLE)

        self.access.role = self.timekeeper_role
        self.access.status = EmployeeAccess.Status.DEACTIVATED
        self.access.save(update_fields=['role', 'status'])
        self._assert_error(ERROR_ACCESS_INACTIVE)

        self.access.status = EmployeeAccess.Status.ACTIVATED
        self.access.is_active = False
        self.access.save(update_fields=['status', 'is_active'])
        self._assert_error(ERROR_ACCESS_INACTIVE)

        self.access.status = EmployeeAccess.Status.BLOCKED
        self.access.is_active = True
        self.access.save(update_fields=['status', 'is_active'])
        self._assert_error(ERROR_ACCESS_BLOCKED)

        self.access.status = EmployeeAccess.Status.ACTIVATED
        self.access.save(update_fields=['status'])
        self.employee.status = Employee.Status.DEACTIVATED
        self.employee.save(update_fields=['status'])
        self._assert_error(ERROR_EMPLOYEE_INACTIVE)

    def test_inactive_role_and_non_exact_admin_alias_are_wrong_role(self):
        self.timekeeper_role.is_active = False
        self.timekeeper_role.save(update_fields=['is_active'])
        self._assert_error(ERROR_ACCESS_WRONG_ROLE)

        admin_role = Role.objects.create(
            code='system_admin',
            name='Администратор без fallback календаря фаз',
        )
        self.access.role = admin_role
        self.access.save(update_fields=['role'])
        self._assert_error(ERROR_ACCESS_WRONG_ROLE)

    def test_manager_oup_dispatcher_clerk_and_arbitrary_roles_are_rejected(self):
        role_codes = (
            'manager',
            'oup',
            'dispatcher',
            'settlement_clerk',
            'brigade-phase-arbitrary-role',
        )
        for index, role_code in enumerate(role_codes, start=1):
            with self.subTest(role_code=role_code):
                role, _created = Role.objects.get_or_create(
                    code=role_code,
                    defaults={
                        'name': f'Запрещённая роль календаря {index}',
                        'is_active': True,
                    },
                )
                employee = Employee.objects.create(
                    full_name=f'Запрещённый actor календаря {index}',
                    status=Employee.Status.ACTIVE,
                    is_active=True,
                )
                access = EmployeeAccess.objects.create(
                    employee=employee,
                    role=role,
                    access_code=f'BPC-DENIED-{index}',
                    status=EmployeeAccess.Status.ACTIVATED,
                    is_active=True,
                )
                self._assert_error(
                    ERROR_ACCESS_WRONG_ROLE,
                    actor_access_id=access.pk,
                )
        self.assertFalse(WatchPeriodBrigadePhaseVersion._base_manager.exists())

    def test_period_and_schedule_failures_are_controlled(self):
        self._assert_error(
            ERROR_WATCH_PERIOD_NOT_FOUND,
            watch_period_id=self.period.pk + 10000,
        )
        self._assert_error(
            ERROR_WORK_SCHEDULE_NOT_FOUND,
            work_schedule_id=self.schedule.pk + 10000,
        )
        self.schedule.is_active = False
        self.schedule.save(update_fields=['is_active'])
        self._assert_error(ERROR_WORK_SCHEDULE_INACTIVE)

    def test_source_validation_and_effective_period_boundary(self):
        for overrides in (
            {'order_number': '   '},
            {'schedule_designation': ''},
            {'order_date': 'not-a-date'},
            {'order_date': '2031-02-02', 'effective_from': '2031-02-01'},
            {'order_document_sha256': 'a' * 63},
            {'schedule_document_sha256': 'z' * 64},
        ):
            with self.subTest(overrides=overrides):
                self._assert_error(ERROR_INVALID_SOURCE, **overrides)
        self._assert_error(ERROR_SOURCE_NOT_EFFECTIVE, effective_from='2031-02-02')
        self.assertEqual(WatchPeriodBrigadePhaseVersion._base_manager.count(), 0)

    def test_brigade_set_requires_exact_structured_complete_rows(self):
        invalid_sets = [
            [{'brigade_number': 1, 'phase': 'day'}],
            [
                {'brigade_number': 1, 'phase': 'day'},
                {'brigade_number': 2, 'phase': 'night'},
                {'brigade_number': 3, 'phase': 'off'},
                {'brigade_number': 5, 'phase': 'off'},
            ],
            [
                {'brigade_number': 1, 'phase': 'day'},
                {'brigade_number': 1, 'phase': 'night'},
                {'brigade_number': 3, 'phase': 'off'},
                {'brigade_number': 4, 'phase': 'off'},
            ],
            [
                {'brigade_number': 1, 'phase': 'invalid'},
                {'brigade_number': 2, 'phase': 'night'},
                {'brigade_number': 3, 'phase': 'off'},
                {'brigade_number': 4, 'phase': 'off'},
            ],
            [
                {'brigade_number': True, 'phase': 'day'},
                {'brigade_number': 2, 'phase': 'night'},
                {'brigade_number': 3, 'phase': 'off'},
                {'brigade_number': 4, 'phase': 'off'},
            ],
            [
                {'brigade_number': 1, 'phase': 'day', 'extra': 'forbidden'},
                {'brigade_number': 2, 'phase': 'night'},
                {'brigade_number': 3, 'phase': 'off'},
                {'brigade_number': 4, 'phase': 'off'},
            ],
            WatchPeriodBrigadePhaseRow(
                brigade_number=1,
                phase=WatchPeriodBrigadePhaseRow.Phase.DAY,
            ),
        ]
        for brigade_phases in invalid_sets:
            with self.subTest(brigade_phases=brigade_phases):
                self._assert_error(
                    ERROR_INVALID_BRIGADE_SET,
                    brigade_phases=brigade_phases,
                )
        self.assertEqual(WatchPeriodBrigadePhaseVersion._base_manager.count(), 0)

        self.schedule.brigade_count = 0
        self.schedule.save(update_fields=['brigade_count'])
        self._assert_error(ERROR_INVALID_BRIGADE_SET, brigade_phases=[])

    def test_universal_validator_accepts_five_brigades_without_special_policy(self):
        normalized = _normalize_brigade_phases(
            [
                {'brigade_number': 5, 'phase': 'off'},
                {'brigade_number': 1, 'phase': 'day'},
                {'brigade_number': 4, 'phase': 'night'},
                {'brigade_number': 2, 'phase': 'off'},
                {'brigade_number': 3, 'phase': 'day'},
            ],
            brigade_count=5,
        )
        self.assertEqual(
            normalized,
            ((1, 'day'), (2, 'off'), (3, 'day'), (4, 'night'), (5, 'off')),
        )

    def test_sequential_retry_is_idempotent_for_same_actor_source_and_phase_set(self):
        first = create_watch_period_brigade_phase_draft(**self._kwargs())
        second = create_watch_period_brigade_phase_draft(
            **self._kwargs(
                order_number='ПРИКАЗ № 17',
                order_document_sha256=self.order_sha.lower(),
                brigade_phases=list(reversed(self._kwargs()['brigade_phases'])),
            )
        )
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(WatchPeriodBrigadePhaseVersion._base_manager.count(), 1)
        self.assertEqual(WatchPeriodBrigadePhaseRow._base_manager.count(), 4)

    def test_changed_source_or_phases_creates_next_draft(self):
        first = create_watch_period_brigade_phase_draft(**self._kwargs())
        second = create_watch_period_brigade_phase_draft(
            **self._kwargs(order_number='Приказ № 18')
        )
        changed_phases = self._kwargs()['brigade_phases']
        changed_phases[0] = {'brigade_number': 4, 'phase': 'night'}
        third = create_watch_period_brigade_phase_draft(
            **self._kwargs(order_number='Приказ № 18', brigade_phases=changed_phases)
        )
        self.assertEqual(
            [first.version_number, second.version_number, third.version_number],
            [1, 2, 3],
        )
        self.assertEqual(WatchPeriodBrigadePhaseVersion._base_manager.count(), 3)
        self.assertEqual(WatchPeriodBrigadePhaseRow._base_manager.count(), 12)

    def test_same_payload_from_another_exact_timekeeper_creates_new_draft(self):
        first = create_watch_period_brigade_phase_draft(**self._kwargs())
        another_employee = Employee.objects.create(
            full_name='Второй табельщик теста календаря фаз',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        another_access = EmployeeAccess.objects.create(
            employee=another_employee,
            role=self.timekeeper_role,
            access_code='BPC-SERVICE-ACCESS-2',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

        second = create_watch_period_brigade_phase_draft(
            **self._kwargs(actor_access_id=another_access.pk)
        )
        self.assertNotEqual(second.pk, first.pk)
        self.assertEqual(second.version_number, 2)
        self.assertEqual(second.created_by_access_id, another_access.pk)

    def test_matching_confirmed_version_is_never_returned_as_a_draft(self):
        confirmed = create_watch_period_brigade_phase_draft(**self._kwargs())
        now = timezone.now()
        WatchPeriodBrigadePhaseVersion._base_manager.filter(pk=confirmed.pk).update(
            status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
            confirmed_by_access=self.access,
            confirmed_at=now,
        )

        draft = create_watch_period_brigade_phase_draft(**self._kwargs())
        self.assertNotEqual(draft.pk, confirmed.pk)
        self.assertEqual(draft.status, WatchPeriodBrigadePhaseVersion.Status.DRAFT)
        self.assertEqual(draft.version_number, 2)
        self.assertEqual(draft.based_on_version_id, confirmed.pk)

    def test_lineage_uses_the_single_current_confirmed_version(self):
        snapshot = {
            'source_kind': 'official_schedule_order',
            'order': {
                'number': 'Исходный приказ',
                'date': '2030-12-01',
                'effective_from': '2031-01-01',
                'document_sha256': 'c' * 64,
            },
            'schedule': {
                'designation': 'Исходный график',
                'document_sha256': 'd' * 64,
            },
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
            ).encode('utf-8')
        ).hexdigest()
        confirmed = WatchPeriodBrigadePhaseVersion(
            watch_period=self.period,
            work_schedule=self.schedule,
            version_number=1,
            status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
            created_by_access=self.access,
            confirmed_by_access=self.access,
            confirmed_at=timezone.now(),
            source_snapshot=snapshot,
            source_fingerprint=fingerprint,
        )
        self._insert(WatchPeriodBrigadePhaseVersion, [confirmed])
        self._insert(
            WatchPeriodBrigadePhaseRow,
            [
                WatchPeriodBrigadePhaseRow(
                    version=confirmed,
                    brigade_number=number,
                    phase=phase,
                )
                for number, phase in ((1, 'night'), (2, 'day'), (3, 'off'), (4, 'off'))
            ],
        )

        draft = create_watch_period_brigade_phase_draft(**self._kwargs())
        self.assertEqual(draft.version_number, 2)
        self.assertEqual(draft.based_on_version_id, confirmed.pk)
        self.assertEqual(draft.status, WatchPeriodBrigadePhaseVersion.Status.DRAFT)

    def test_row_writer_failure_rolls_back_version_and_is_controlled(self):
        with mock.patch(
            'shifts.brigade_phase_calendar._trusted_insert_rows',
            side_effect=IntegrityError('simulated row insert failure'),
        ):
            self._assert_error(ERROR_INCONSISTENT_GRAPH)
        self.assertEqual(WatchPeriodBrigadePhaseVersion._base_manager.count(), 0)
        self.assertEqual(WatchPeriodBrigadePhaseRow._base_manager.count(), 0)

    def test_command_does_not_modify_actor_schedule_or_period(self):
        employee_before = {
            'status': self.employee.status,
            'is_active': self.employee.is_active,
            'work_schedule_id': self.employee.work_schedule_id,
            'brigade_number': self.employee.brigade_number,
        }
        schedule_before = {
            'code': self.schedule.code,
            'name': self.schedule.name,
            'brigade_count': self.schedule.brigade_count,
            'is_active': self.schedule.is_active,
        }
        period_before = {
            'name': self.period.name,
            'starts_on': self.period.starts_on,
            'ends_on': self.period.ends_on,
            'is_active': self.period.is_active,
        }

        create_watch_period_brigade_phase_draft(**self._kwargs())
        self.employee.refresh_from_db()
        self.schedule.refresh_from_db()
        self.period.refresh_from_db()

        self.assertEqual(
            {
                'status': self.employee.status,
                'is_active': self.employee.is_active,
                'work_schedule_id': self.employee.work_schedule_id,
                'brigade_number': self.employee.brigade_number,
            },
            employee_before,
        )
        self.assertEqual(
            {
                'code': self.schedule.code,
                'name': self.schedule.name,
                'brigade_count': self.schedule.brigade_count,
                'is_active': self.schedule.is_active,
            },
            schedule_before,
        )
        self.assertEqual(
            {
                'name': self.period.name,
                'starts_on': self.period.starts_on,
                'ends_on': self.period.ends_on,
                'is_active': self.period.is_active,
            },
            period_before,
        )


class BrigadePhaseCalendarConfirmationServiceTests(TestCase):
    order_sha = 'c' * 64
    schedule_sha = 'd' * 64

    def setUp(self):
        self.timekeeper_role = Role.objects.get(code='timekeeper')
        self.admin_role, _created = Role.objects.get_or_create(
            code='admin',
            defaults={'name': 'Системный администратор', 'is_active': True},
        )
        self.employee = Employee.objects.create(
            full_name='Табельщик подтверждения календаря фаз',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.timekeeper_role,
            access_code='BPC-CONFIRM-ACCESS',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.admin_employee = Employee.objects.create(
            full_name='Администратор подтверждения календаря фаз',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.admin_employee,
            role=self.admin_role,
            access_code='BPC-CONFIRM-ADMIN',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.schedule_11 = WorkSchedule.objects.get(code=WORK_SCHEDULE_CODE_11)
        self.schedule_12 = WorkSchedule.objects.get(code=WORK_SCHEDULE_CODE_12)
        self.period = WatchPeriod.objects.create(
            name='Период подтверждения календаря фаз',
            starts_on=date(2032, 1, 15),
            ends_on=date(2032, 2, 28),
        )

    def _draft(
        self,
        *,
        schedule=None,
        designation='График № 12/1',
        phases=None,
        order_number='Приказ подтверждения № 1',
        actor_access=None,
        period=None,
    ):
        schedule = schedule or self.schedule_12
        if phases is None:
            phases = [
                {'brigade_number': 1, 'phase': 'night'},
                {'brigade_number': 2, 'phase': 'day'},
                {'brigade_number': 3, 'phase': 'off'},
                {'brigade_number': 4, 'phase': 'off'},
            ]
        return create_watch_period_brigade_phase_draft(
            watch_period_id=(period or self.period).pk,
            work_schedule_id=schedule.pk,
            actor_access_id=(actor_access or self.access).pk,
            order_number=order_number,
            order_date='2032-01-01',
            effective_from='2032-01-15',
            order_document_sha256=self.order_sha,
            schedule_designation=designation,
            schedule_document_sha256=self.schedule_sha,
            brigade_phases=phases,
        )

    def _confirm(self, version, *, actor_access=None):
        return confirm_watch_period_brigade_phase_version(
            version_id=version.pk,
            actor_access_id=(actor_access or self.access).pk,
        )

    def _assert_confirm_error(self, code, version_id, *, actor_access=None):
        with self.assertRaises(BrigadePhaseCalendarError) as caught:
            confirm_watch_period_brigade_phase_version(
                version_id=version_id,
                actor_access_id=(actor_access or self.access).pk,
            )
        self.assertEqual(caught.exception.code, code)

    def _another_timekeeper_access(self):
        employee = Employee.objects.create(
            full_name='Второй табельщик подтверждения календаря фаз',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        return EmployeeAccess.objects.create(
            employee=employee,
            role=self.timekeeper_role,
            access_code='BPC-CONFIRM-ACCESS-2',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    def test_policy_uses_exact_codes_seeded_by_users_0013(self):
        migration = import_module('users.migrations.0013_normalize_department_work_schedule')
        seeded = {
            code: (name, brigade_count)
            for code, name, brigade_count in migration.WORK_SCHEDULES
        }
        self.assertEqual(WORK_SCHEDULE_CODE_11, 'schedule_11')
        self.assertEqual(WORK_SCHEDULE_CODE_12, 'schedule_12')
        self.assertEqual(seeded[WORK_SCHEDULE_CODE_11][1], 2)
        self.assertEqual(seeded[WORK_SCHEDULE_CODE_12][1], 4)
        self.assertEqual(self.schedule_11.brigade_count, 2)
        self.assertEqual(self.schedule_12.brigade_count, 4)

    def test_first_confirmation_succeeds_for_official_schedule_12(self):
        draft = self._draft(designation='  ГРАФИК  №  12 / 1  ')
        confirmed = self._confirm(draft)
        confirmed.refresh_from_db()

        self.assertEqual(confirmed.status, WatchPeriodBrigadePhaseVersion.Status.CONFIRMED)
        self.assertEqual(confirmed.confirmed_by_access_id, self.access.pk)
        self.assertIsNotNone(confirmed.confirmed_at)
        self.assertIsNone(confirmed.superseded_by_access_id)
        self.assertIsNone(confirmed.superseded_at)
        self.assertEqual(
            WatchPeriodBrigadePhaseVersion._base_manager.filter(
                watch_period=self.period,
                work_schedule=self.schedule_12,
                status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
            ).count(),
            1,
        )

    def test_first_confirmation_succeeds_for_official_schedule_11(self):
        draft = self._draft(
            schedule=self.schedule_11,
            designation='График № 11/1',
            phases=[
                {'brigade_number': 1, 'phase': 'day'},
                {'brigade_number': 2, 'phase': 'off'},
            ],
        )
        confirmed = self._confirm(draft)
        confirmed.refresh_from_db()
        self.assertEqual(confirmed.status, WatchPeriodBrigadePhaseVersion.Status.CONFIRMED)
        self.assertEqual(confirmed.confirmed_by_access_id, self.access.pk)

    def test_exact_admin_confirms_and_supersedes_with_actual_audit_actor(self):
        first = self._draft(actor_access=self.admin_access)
        self.assertEqual(first.created_by_access_id, self.admin_access.pk)
        first = self._confirm(first, actor_access=self.admin_access)
        self.assertEqual(first.confirmed_by_access_id, self.admin_access.pk)

        replacement = self._draft(
            actor_access=self.admin_access,
            order_number='Приказ подтверждения admin № 2',
        )
        replacement = self._confirm(
            replacement,
            actor_access=self.admin_access,
        )
        first.refresh_from_db()
        replacement.refresh_from_db()

        self.assertEqual(first.superseded_by_access_id, self.admin_access.pk)
        self.assertEqual(replacement.created_by_access_id, self.admin_access.pk)
        self.assertEqual(replacement.confirmed_by_access_id, self.admin_access.pk)

    def test_official_phase_distribution_is_enforced(self):
        invalid_12 = self._draft(
            phases=[
                {'brigade_number': 1, 'phase': 'day'},
                {'brigade_number': 2, 'phase': 'day'},
                {'brigade_number': 3, 'phase': 'off'},
                {'brigade_number': 4, 'phase': 'off'},
            ],
        )
        self._assert_confirm_error(ERROR_POLICY_MISMATCH, invalid_12.pk)
        invalid_12.refresh_from_db()
        self.assertEqual(invalid_12.status, WatchPeriodBrigadePhaseVersion.Status.DRAFT)

        another_period = WatchPeriod.objects.create(
            name='Период неверной policy № 11/1',
            starts_on=self.period.starts_on,
            ends_on=self.period.ends_on,
        )
        invalid_11 = self._draft(
            period=another_period,
            schedule=self.schedule_11,
            designation='График № 11/1',
            phases=[
                {'brigade_number': 1, 'phase': 'night'},
                {'brigade_number': 2, 'phase': 'off'},
            ],
        )
        self._assert_confirm_error(ERROR_POLICY_MISMATCH, invalid_11.pk)

    def test_designation_must_match_exact_work_schedule_code_policy(self):
        draft = self._draft(designation='График № 11/1')
        self._assert_confirm_error(
            ERROR_SCHEDULE_DESIGNATION_MISMATCH,
            draft.pk,
        )

    def test_unsupported_schedule_is_fail_closed(self):
        unsupported = WorkSchedule.objects.create(
            code='schedule-confirmation-policy-not-defined',
            name='График без утверждённой policy',
            brigade_count=2,
            is_active=True,
        )
        draft = self._draft(
            schedule=unsupported,
            designation='График № 99/1',
            phases=[
                {'brigade_number': 1, 'phase': 'day'},
                {'brigade_number': 2, 'phase': 'off'},
            ],
        )
        self._assert_confirm_error(ERROR_POLICY_NOT_DEFINED, draft.pk)

    def test_corrupted_source_snapshot_is_rejected(self):
        draft = self._draft()
        corrupted = dict(draft.source_snapshot)
        corrupted['unexpected'] = 'forbidden'
        WatchPeriodBrigadePhaseVersion._base_manager.filter(pk=draft.pk).update(
            source_snapshot=corrupted,
        )
        self._assert_confirm_error(ERROR_SOURCE_INVALID, draft.pk)
        draft.refresh_from_db()
        self.assertEqual(draft.status, WatchPeriodBrigadePhaseVersion.Status.DRAFT)

    def test_corrupted_source_fingerprint_is_rejected(self):
        draft = self._draft()
        WatchPeriodBrigadePhaseVersion._base_manager.filter(pk=draft.pk).update(
            source_fingerprint='f' * 64,
        )
        self._assert_confirm_error(ERROR_SOURCE_FINGERPRINT_INVALID, draft.pk)

    def test_graph_incomplete_and_inconsistent_are_distinct(self):
        incomplete = self._draft()
        WatchPeriodBrigadePhaseRow._base_manager.filter(
            version=incomplete,
            brigade_number=4,
        ).delete()
        self._assert_confirm_error(ERROR_GRAPH_INCOMPLETE, incomplete.pk)

        another_period = WatchPeriod.objects.create(
            name='Период повреждённого графа',
            starts_on=self.period.starts_on,
            ends_on=self.period.ends_on,
        )
        inconsistent = self._draft(
            period=another_period,
            order_number='Приказ повреждённого графа',
        )
        WatchPeriodBrigadePhaseRow._base_manager.filter(
            version=inconsistent,
            brigade_number=4,
        ).update(brigade_number=5)
        self._assert_confirm_error(ERROR_GRAPH_INCONSISTENT, inconsistent.pk)

    def test_confirmation_requires_exact_active_timekeeper_access(self):
        draft = self._draft()
        wrong_role = Role.objects.create(
            code='brigade-phase-confirm-wrong-role',
            name='Неверная роль подтверждения календаря фаз',
        )
        wrong_employee = Employee.objects.create(
            full_name='Не табельщик подтверждения календаря фаз',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        wrong_access = EmployeeAccess.objects.create(
            employee=wrong_employee,
            role=wrong_role,
            access_code='BPC-CONFIRM-WRONG-ROLE',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self._assert_confirm_error(
            ERROR_ACCESS_WRONG_ROLE,
            draft.pk,
            actor_access=wrong_access,
        )
        self._assert_confirm_error(
            ERROR_ACCESS_NOT_FOUND,
            draft.pk,
            actor_access=type('MissingAccess', (), {'pk': wrong_access.pk + 10000})(),
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, WatchPeriodBrigadePhaseVersion.Status.DRAFT)

    def test_atomic_replacement_uses_one_server_time_and_exact_actor(self):
        first = self._confirm(self._draft())
        replacement = self._draft(order_number='Приказ подтверждения № 2')
        replacement_creator_id = replacement.created_by_access_id
        confirming_access = self._another_timekeeper_access()

        confirmed = self._confirm(replacement, actor_access=confirming_access)
        first.refresh_from_db()
        confirmed.refresh_from_db()

        self.assertEqual(first.status, WatchPeriodBrigadePhaseVersion.Status.SUPERSEDED)
        self.assertEqual(first.superseded_by_access_id, confirming_access.pk)
        self.assertEqual(confirmed.status, WatchPeriodBrigadePhaseVersion.Status.CONFIRMED)
        self.assertEqual(confirmed.confirmed_by_access_id, confirming_access.pk)
        self.assertEqual(first.superseded_at, confirmed.confirmed_at)
        self.assertEqual(confirmed.created_by_access_id, replacement_creator_id)
        self.assertEqual(
            WatchPeriodBrigadePhaseVersion._base_manager.filter(
                watch_period=self.period,
                work_schedule=self.schedule_12,
                status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
            ).count(),
            1,
        )

    def test_repeat_confirmation_is_idempotent_without_actor_or_time_change(self):
        confirmed = self._confirm(self._draft())
        confirmed.refresh_from_db()
        original_at = confirmed.confirmed_at
        original_actor_id = confirmed.confirmed_by_access_id
        version_count = WatchPeriodBrigadePhaseVersion._base_manager.count()
        row_count = WatchPeriodBrigadePhaseRow._base_manager.count()

        repeated = self._confirm(
            confirmed,
            actor_access=self._another_timekeeper_access(),
        )
        repeated.refresh_from_db()
        self.assertEqual(repeated.pk, confirmed.pk)
        self.assertEqual(repeated.confirmed_at, original_at)
        self.assertEqual(repeated.confirmed_by_access_id, original_actor_id)
        self.assertEqual(WatchPeriodBrigadePhaseVersion._base_manager.count(), version_count)
        self.assertEqual(WatchPeriodBrigadePhaseRow._base_manager.count(), row_count)

    def test_second_competing_draft_on_old_lineage_is_stale(self):
        original = self._confirm(self._draft())
        first_competing = self._draft(order_number='Конкурирующий приказ № 1')
        second_competing = self._draft(order_number='Конкурирующий приказ № 2')
        self.assertEqual(first_competing.based_on_version_id, original.pk)
        self.assertEqual(second_competing.based_on_version_id, original.pk)

        self._confirm(first_competing)
        self._assert_confirm_error(ERROR_VERSION_STALE, second_competing.pk)
        second_competing.refresh_from_db()
        self.assertEqual(
            second_competing.status,
            WatchPeriodBrigadePhaseVersion.Status.DRAFT,
        )
        original.refresh_from_db()
        self._assert_confirm_error(ERROR_VERSION_STALE, original.pk)

    def test_failure_after_supersede_rolls_back_both_versions(self):
        current = self._confirm(self._draft())
        current.refresh_from_db()
        original_confirmed_at = current.confirmed_at
        replacement = self._draft(order_number='Приказ rollback подтверждения')

        with mock.patch(
            'shifts.brigade_phase_calendar._trusted_confirm_version',
            side_effect=IntegrityError('simulated confirmation failure'),
        ):
            self._assert_confirm_error(ERROR_GRAPH_INCONSISTENT, replacement.pk)

        current.refresh_from_db()
        replacement.refresh_from_db()
        self.assertEqual(current.status, WatchPeriodBrigadePhaseVersion.Status.CONFIRMED)
        self.assertEqual(current.confirmed_at, original_confirmed_at)
        self.assertIsNone(current.superseded_at)
        self.assertIsNone(current.superseded_by_access_id)
        self.assertEqual(replacement.status, WatchPeriodBrigadePhaseVersion.Status.DRAFT)
        self.assertIsNone(replacement.confirmed_at)
        self.assertIsNone(replacement.confirmed_by_access_id)

    def test_confirmation_preserves_provenance_creator_and_rows(self):
        draft = self._draft()
        snapshot = json.loads(json.dumps(draft.source_snapshot))
        fingerprint = draft.source_fingerprint
        creator_id = draft.created_by_access_id
        rows = list(
            WatchPeriodBrigadePhaseRow._base_manager.filter(version=draft)
            .order_by('brigade_number')
            .values_list('pk', 'brigade_number', 'phase')
        )

        self._confirm(draft)
        draft.refresh_from_db()
        self.assertEqual(draft.source_snapshot, snapshot)
        self.assertEqual(draft.source_fingerprint, fingerprint)
        self.assertEqual(draft.created_by_access_id, creator_id)
        self.assertEqual(
            list(
                WatchPeriodBrigadePhaseRow._base_manager.filter(version=draft)
                .order_by('brigade_number')
                .values_list('pk', 'brigade_number', 'phase')
            ),
            rows,
        )

    def test_version_not_found_is_controlled(self):
        self._assert_confirm_error(ERROR_VERSION_NOT_FOUND, 999999)

    def _resolve(self, brigade_number, *, period=None, schedule=None):
        return resolve_confirmed_brigade_phase(
            watch_period_id=(period or self.period).pk,
            work_schedule_id=(schedule or self.schedule_12).pk,
            brigade_number=brigade_number,
        )

    def _assert_resolve_error(
        self,
        code,
        brigade_number,
        *,
        period=None,
        schedule=None,
    ):
        with self.assertRaises(BrigadePhaseCalendarError) as caught:
            self._resolve(
                brigade_number,
                period=period,
                schedule=schedule,
            )
        self.assertEqual(caught.exception.code, code)

    def _force_confirm_for_resolver(self, draft):
        WatchPeriodBrigadePhaseVersion._base_manager.filter(pk=draft.pk).update(
            status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
            confirmed_by_access=self.access,
            confirmed_at=timezone.now(),
        )
        draft.refresh_from_db()
        return draft

    def test_resolver_returns_immutable_exact_day_night_and_off_results(self):
        confirmed = self._confirm(self._draft())
        rows_by_number = {
            row.brigade_number: row
            for row in WatchPeriodBrigadePhaseRow._base_manager.filter(
                version=confirmed,
            )
        }

        night = self._resolve(1)
        day = self._resolve(2)
        off = self._resolve(3)

        self.assertIsInstance(day, ConfirmedBrigadePhase)
        self.assertEqual(night.phase, WatchPeriodBrigadePhaseRow.Phase.NIGHT)
        self.assertEqual(day.phase, WatchPeriodBrigadePhaseRow.Phase.DAY)
        self.assertEqual(off.phase, WatchPeriodBrigadePhaseRow.Phase.OFF)
        self.assertEqual(day.version_id, confirmed.pk)
        self.assertEqual(day.row_id, rows_by_number[2].pk)
        self.assertEqual(day.watch_period_id, self.period.pk)
        self.assertEqual(day.work_schedule_id, self.schedule_12.pk)
        self.assertEqual(day.brigade_number, 2)
        self.assertEqual(day.source_fingerprint, confirmed.source_fingerprint)
        self.assertFalse(hasattr(day, 'source_snapshot'))
        self.assertFalse(hasattr(day, 'confirmed_by_access'))
        with self.assertRaises(FrozenInstanceError):
            day.phase = WatchPeriodBrigadePhaseRow.Phase.OFF

    def test_resolver_ignores_draft_and_requires_confirmed_version(self):
        self._draft()
        self._assert_resolve_error(ERROR_CONFIRMED_VERSION_NOT_FOUND, 1)

    def test_resolver_ignores_superseded_and_uses_replacement_only(self):
        original = self._confirm(self._draft())
        replacement = self._draft(
            order_number='Приказ resolver replacement',
            phases=[
                {'brigade_number': 1, 'phase': 'day'},
                {'brigade_number': 2, 'phase': 'night'},
                {'brigade_number': 3, 'phase': 'off'},
                {'brigade_number': 4, 'phase': 'off'},
            ],
        )
        replacement = self._confirm(replacement)
        original.refresh_from_db()
        self.assertEqual(
            original.status,
            WatchPeriodBrigadePhaseVersion.Status.SUPERSEDED,
        )

        resolved = self._resolve(1)
        self.assertEqual(resolved.version_id, replacement.pk)
        self.assertEqual(resolved.phase, WatchPeriodBrigadePhaseRow.Phase.DAY)

    def test_resolver_rejects_invalid_brigade_number(self):
        self._confirm(self._draft())
        for brigade_number in (True, 0, 5, '1', None):
            with self.subTest(brigade_number=brigade_number):
                self._assert_resolve_error(
                    ERROR_BRIGADE_NOT_FOUND,
                    brigade_number,
                )

    def test_resolver_rejects_corrupted_snapshot_and_fingerprint(self):
        snapshot_version = self._confirm(self._draft())
        corrupted = dict(snapshot_version.source_snapshot)
        corrupted['unexpected'] = 'forbidden'
        WatchPeriodBrigadePhaseVersion._base_manager.filter(
            pk=snapshot_version.pk,
        ).update(source_snapshot=corrupted)
        self._assert_resolve_error(ERROR_SOURCE_INVALID, 1)

        fingerprint_period = WatchPeriod.objects.create(
            name='Период resolver fingerprint',
            starts_on=self.period.starts_on,
            ends_on=self.period.ends_on,
        )
        fingerprint_version = self._confirm(
            self._draft(
                period=fingerprint_period,
                order_number='Приказ resolver fingerprint',
            )
        )
        WatchPeriodBrigadePhaseVersion._base_manager.filter(
            pk=fingerprint_version.pk,
        ).update(source_fingerprint='e' * 64)
        self._assert_resolve_error(
            ERROR_SOURCE_FINGERPRINT_INVALID,
            1,
            period=fingerprint_period,
        )

    def test_resolver_rejects_incomplete_and_inconsistent_graph(self):
        incomplete = self._confirm(self._draft())
        WatchPeriodBrigadePhaseRow._base_manager.filter(
            version=incomplete,
            brigade_number=4,
        ).delete()
        self._assert_resolve_error(ERROR_GRAPH_INCOMPLETE, 1)

        inconsistent_period = WatchPeriod.objects.create(
            name='Период resolver inconsistent graph',
            starts_on=self.period.starts_on,
            ends_on=self.period.ends_on,
        )
        inconsistent = self._confirm(
            self._draft(
                period=inconsistent_period,
                order_number='Приказ resolver inconsistent graph',
            )
        )
        WatchPeriodBrigadePhaseRow._base_manager.filter(
            version=inconsistent,
            brigade_number=4,
        ).update(brigade_number=5)
        self._assert_resolve_error(
            ERROR_GRAPH_INCONSISTENT,
            1,
            period=inconsistent_period,
        )

    def test_resolver_rejects_unsupported_code_and_designation_mismatch(self):
        unsupported = WorkSchedule.objects.create(
            code='resolver-policy-not-defined',
            name='Resolver график без policy',
            brigade_count=2,
            is_active=True,
        )
        unsupported_draft = self._draft(
            schedule=unsupported,
            designation='График № 99/1',
            phases=[
                {'brigade_number': 1, 'phase': 'day'},
                {'brigade_number': 2, 'phase': 'off'},
            ],
        )
        self._force_confirm_for_resolver(unsupported_draft)
        self._assert_resolve_error(
            ERROR_POLICY_NOT_DEFINED,
            1,
            schedule=unsupported,
        )

        mismatch_period = WatchPeriod.objects.create(
            name='Период resolver designation mismatch',
            starts_on=self.period.starts_on,
            ends_on=self.period.ends_on,
        )
        mismatch = self._draft(
            period=mismatch_period,
            designation='График № 11/1',
            order_number='Приказ resolver designation mismatch',
        )
        self._force_confirm_for_resolver(mismatch)
        self._assert_resolve_error(
            ERROR_SCHEDULE_DESIGNATION_MISMATCH,
            1,
            period=mismatch_period,
        )

    def test_resolver_rejects_official_policy_mismatch(self):
        invalid = self._draft(
            phases=[
                {'brigade_number': 1, 'phase': 'day'},
                {'brigade_number': 2, 'phase': 'day'},
                {'brigade_number': 3, 'phase': 'off'},
                {'brigade_number': 4, 'phase': 'off'},
            ],
        )
        self._force_confirm_for_resolver(invalid)
        self._assert_resolve_error(ERROR_POLICY_MISMATCH, 1)

    def test_resolver_maps_model_audit_shape_failure_to_controlled_error(self):
        self._confirm(self._draft())
        with mock.patch.object(
            WatchPeriodBrigadePhaseVersion,
            'full_clean',
            side_effect=ValidationError('simulated audit-shape failure'),
        ):
            self._assert_resolve_error(
                ERROR_CONFIRMED_VERSION_INCONSISTENT,
                1,
            )

    def test_resolver_missing_exact_period_or_schedule_is_controlled(self):
        with self.assertRaises(BrigadePhaseCalendarError) as period_error:
            resolve_confirmed_brigade_phase(
                watch_period_id=self.period.pk + 10000,
                work_schedule_id=self.schedule_12.pk,
                brigade_number=1,
            )
        self.assertEqual(period_error.exception.code, ERROR_WATCH_PERIOD_NOT_FOUND)

        with self.assertRaises(BrigadePhaseCalendarError) as schedule_error:
            resolve_confirmed_brigade_phase(
                watch_period_id=self.period.pk,
                work_schedule_id=self.schedule_12.pk + 10000,
                brigade_number=1,
            )
        self.assertEqual(schedule_error.exception.code, ERROR_WORK_SCHEDULE_NOT_FOUND)

    def test_repeated_resolver_call_is_fully_read_only(self):
        self._confirm(self._draft())
        version_before = list(
            WatchPeriodBrigadePhaseVersion._base_manager.order_by('pk').values()
        )
        rows_before = list(
            WatchPeriodBrigadePhaseRow._base_manager.order_by('pk').values()
        )
        employee_before = list(
            Employee.objects.filter(pk=self.employee.pk).values()
        )
        access_before = list(
            EmployeeAccess.objects.filter(pk=self.access.pk).values()
        )

        first = self._resolve(2)
        second = self._resolve(2)

        self.assertEqual(first, second)
        self.assertEqual(
            list(WatchPeriodBrigadePhaseVersion._base_manager.order_by('pk').values()),
            version_before,
        )
        self.assertEqual(
            list(WatchPeriodBrigadePhaseRow._base_manager.order_by('pk').values()),
            rows_before,
        )
        self.assertEqual(
            list(Employee.objects.filter(pk=self.employee.pk).values()),
            employee_before,
        )
        self.assertEqual(
            list(EmployeeAccess.objects.filter(pk=self.access.pk).values()),
            access_before,
        )
