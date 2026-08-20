import hashlib
import inspect
import json
from datetime import date
from unittest import mock

from django.db import IntegrityError, models
from django.test import TestCase
from django.utils import timezone

from users.models import Employee, EmployeeAccess, Role, WorkSchedule

from .brigade_phase_calendar import (
    ERROR_ACCESS_BLOCKED,
    ERROR_ACCESS_INACTIVE,
    ERROR_ACCESS_NOT_FOUND,
    ERROR_ACCESS_WRONG_ROLE,
    ERROR_EMPLOYEE_INACTIVE,
    ERROR_INCONSISTENT_GRAPH,
    ERROR_INVALID_BRIGADE_SET,
    ERROR_INVALID_SOURCE,
    ERROR_SOURCE_NOT_EFFECTIVE,
    ERROR_WATCH_PERIOD_NOT_FOUND,
    ERROR_WORK_SCHEDULE_INACTIVE,
    ERROR_WORK_SCHEDULE_NOT_FOUND,
    BrigadePhaseCalendarError,
    _normalize_brigade_phases,
    create_watch_period_brigade_phase_draft,
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

    def test_inactive_role_is_wrong_role_and_no_admin_fallback_exists(self):
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
