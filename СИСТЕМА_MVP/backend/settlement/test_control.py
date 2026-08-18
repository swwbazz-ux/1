import json
import inspect
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import timedelta
from unittest import mock

from django.contrib.staticfiles import finders
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone

from references.models import Dormitory
from shifts.models import WatchPeriod
from users import employee_access_locks
from users.models import Employee, EmployeeAccess, Role, WatchComposition

from . import control as control_module
from . import residents as settlement_residents
from .control import (
    ControlLeaseGrant,
    SettlementControlWriteContext,
    acquire_control_lease,
    ensure_control_lease,
    expire_control_lease,
    heartbeat_control_lease,
    release_control_lease,
    takeover_control_lease,
)
from .models import (
    EmployeeBedOccupancy,
    PhysicalBed,
    PhysicalRoom,
    SettlementControlEvent,
    SettlementControlLease,
    SettlementPreviewApplication,
    SettlementPreviewApplicationItem,
    SettlementResident,
)
from .services import (
    relocate_employee_to_bed,
    release_employee_from_bed,
    settle_employee_on_bed,
)
from . import services as settlement_services
from .tests import _create_manual_shift_application


class SettlementControlFixtureMixin:
    @classmethod
    def create_access(
        cls,
        suffix,
        role,
        *,
        access_status=EmployeeAccess.Status.ACTIVATED,
        access_is_active=True,
        employee_status=Employee.Status.ACTIVE,
        employee_is_active=True,
    ):
        employee = Employee.objects.create(
            full_name=f'Control lifecycle {suffix}',
            personnel_number=f'CONTROL-{suffix}',
            status=employee_status,
            is_active=employee_is_active,
        )
        return EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code=f'CONTROL-ACCESS-{suffix}',
            status=access_status,
            is_active=access_is_active,
        )

    @classmethod
    def create_control_fixtures(cls):
        cls.clerk_role = Role.objects.create(
            code='settlement_clerk',
            name='Control lifecycle clerk',
        )
        cls.admin_role = Role.objects.create(
            code='admin',
            name='Control lifecycle admin',
        )
        cls.other_role = Role.objects.create(
            code='control_lifecycle_other',
            name='Control lifecycle unrelated role',
        )
        cls.clerk_access = cls.create_access('CLERK', cls.clerk_role)
        cls.second_clerk_access = cls.create_access(
            'SECOND-CLERK',
            cls.clerk_role,
        )
        cls.admin_access = cls.create_access('ADMIN', cls.admin_role)

    @staticmethod
    def validation_codes(error):
        if hasattr(error, 'error_dict'):
            return {
                item.code
                for items in error.error_dict.values()
                for item in items
            }
        return {item.code for item in error.error_list}

    def assert_control_error(self, expected_code, callable_, /, *args, **kwargs):
        with self.assertRaises(ValidationError) as context:
            callable_(*args, **kwargs)
        self.assertEqual(self.validation_codes(context.exception), {expected_code})
        return context.exception

    def assert_error_contains_no_secrets(self, error, *secrets):
        rendered_error = str(error)
        for secret in secrets:
            self.assertNotIn(str(secret), rendered_error)

    @staticmethod
    def lease_state():
        lease = SettlementControlLease.objects.get(scope='settlement')
        return {
            'owner_access_id': lease.owner_access_id,
            'owner_session_hash': lease.owner_session_hash,
            'lease_token': lease.lease_token,
            'fencing_revision': lease.fencing_revision,
            'acquired_at': lease.acquired_at,
            'heartbeat_at': lease.heartbeat_at,
            'expires_at': lease.expires_at,
        }

    @staticmethod
    def event_state():
        return list(
            SettlementControlEvent.objects
            .filter(scope='settlement')
            .order_by('pk')
            .values(
                'event_type',
                'actor_access_id',
                'previous_owner_access_id',
                'new_owner_access_id',
                'reason',
                'source',
                'previous_fencing_revision',
                'new_fencing_revision',
                'session_metadata',
            )
        )


class SettlementControlLifecycleTests(
    SettlementControlFixtureMixin,
    TestCase,
):
    @classmethod
    def setUpTestData(cls):
        cls.create_control_fixtures()

    def setUp(self):
        SettlementControlLease.objects.filter(scope='settlement').update(
            owner_access=None,
            owner_session_hash='',
            lease_token=None,
            fencing_revision=0,
            acquired_at=None,
            heartbeat_at=None,
            expires_at=None,
        )
        self.now = timezone.now()

    def acquire(
        self,
        *,
        access=None,
        session='control-session-one',
        now=None,
        ttl=timedelta(minutes=5),
        source='test-control',
        metadata=None,
    ):
        return acquire_control_lease(
            owner_access_id=(access or self.clerk_access).pk,
            raw_session_key=session,
            source=source,
            session_metadata=metadata,
            now=now or self.now,
            ttl=ttl,
        )

    def takeover(
        self,
        *,
        access=None,
        session='control-admin-session',
        reason='Administrative handover approved',
        now=None,
        ttl=timedelta(minutes=5),
        source='test-takeover',
        metadata=None,
    ):
        return takeover_control_lease(
            admin_access_id=(access or self.admin_access).pk,
            raw_session_key=session,
            reason=reason,
            source=source,
            session_metadata=metadata,
            now=now or self.now,
            ttl=ttl,
        )

    def assert_fully_free(self, *, revision):
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertIsNone(lease.owner_access_id)
        self.assertEqual(lease.owner_session_hash, '')
        self.assertIsNone(lease.lease_token)
        self.assertEqual(lease.fencing_revision, revision)
        self.assertIsNone(lease.acquired_at)
        self.assertIsNone(lease.heartbeat_at)
        self.assertIsNone(lease.expires_at)

    def test_ensure_recreates_missing_free_singleton_without_event(self):
        SettlementControlLease.objects.filter(scope='settlement').delete()

        lease = ensure_control_lease()

        self.assertEqual(lease.scope, 'settlement')
        self.assert_fully_free(revision=0)
        self.assertEqual(SettlementControlLease.objects.count(), 1)
        self.assertFalse(SettlementControlEvent.objects.exists())

    def test_ensure_preserves_existing_held_state_and_revision(self):
        grant = self.acquire()
        before = self.lease_state()
        before_events = self.event_state()

        ensured = ensure_control_lease()

        self.assertEqual(ensured.pk, SettlementControlLease.objects.get().pk)
        self.assertEqual(self.lease_state(), before)
        self.assertEqual(self.event_state(), before_events)
        self.assertEqual(ensured.fencing_revision, grant.fencing_revision)

    def test_free_acquire_returns_frozen_grant_and_creates_exact_event(self):
        grant = self.acquire(
            metadata={
                'session_kind': 'django',
                'request_id': 'request-001',
            },
        )

        self.assertIsInstance(grant, ControlLeaseGrant)
        with self.assertRaises(FrozenInstanceError):
            grant.fencing_revision = 999
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.owner_access_id, self.clerk_access.pk)
        self.assertNotEqual(lease.owner_session_hash, 'control-session-one')
        self.assertEqual(lease.lease_token, grant.lease_token)
        self.assertEqual(lease.fencing_revision, grant.fencing_revision)
        self.assertEqual(grant.fencing_revision, 1)
        self.assertEqual(lease.acquired_at, self.now)
        self.assertEqual(lease.heartbeat_at, self.now)
        self.assertEqual(lease.expires_at, self.now + timedelta(minutes=5))
        self.assertEqual(grant.expires_at, lease.expires_at)
        self.assertEqual(self.event_state(), [{
            'event_type': SettlementControlEvent.EventType.ACQUIRED,
            'actor_access_id': self.clerk_access.pk,
            'previous_owner_access_id': None,
            'new_owner_access_id': self.clerk_access.pk,
            'reason': '',
            'source': 'test-control',
            'previous_fencing_revision': 0,
            'new_fencing_revision': 1,
            'session_metadata': {
                'session_kind': 'django',
                'request_id': 'request-001',
            },
        }])

    def test_session_hash_depends_on_secret_key_and_never_stores_raw_key(self):
        raw_session_key = 'raw-session-value-never-persisted'
        with self.settings(SECRET_KEY='control-hash-secret-a'):
            first = self.acquire(session=raw_session_key)
            first_hash = SettlementControlLease.objects.get().owner_session_hash
            release_control_lease(
                owner_access_id=self.clerk_access.pk,
                raw_session_key=raw_session_key,
                lease_token=first.lease_token,
                fencing_revision=first.fencing_revision,
                source='test-release',
                now=self.now + timedelta(seconds=1),
            )

        with self.settings(SECRET_KEY='control-hash-secret-b'):
            self.acquire(
                session=raw_session_key,
                now=self.now + timedelta(seconds=2),
            )
            second_hash = SettlementControlLease.objects.get().owner_session_hash

        self.assertNotEqual(first_hash, second_hash)
        self.assertNotEqual(first_hash, raw_session_key)
        self.assertNotEqual(second_hash, raw_session_key)
        persisted = json.dumps(
            self.event_state(),
            ensure_ascii=False,
            default=str,
        )
        self.assertNotIn(raw_session_key, persisted)

    def test_repeated_acquire_for_same_access_and_session_is_idempotent(self):
        first = self.acquire()
        later = self.now + timedelta(minutes=1)
        with mock.patch(
            'settlement.control.constant_time_compare',
            wraps=__import__(
                'django.utils.crypto',
                fromlist=['constant_time_compare'],
            ).constant_time_compare,
        ) as constant_time_compare:
            second = self.acquire(now=later)

        self.assertTrue(constant_time_compare.called)
        self.assertEqual(second.lease_token, first.lease_token)
        self.assertEqual(second.fencing_revision, first.fencing_revision)
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.acquired_at, self.now)
        self.assertEqual(lease.heartbeat_at, later)
        self.assertEqual(lease.expires_at, later + timedelta(minutes=5))
        self.assertEqual(SettlementControlEvent.objects.count(), 1)

    def test_other_access_or_other_session_gets_busy_without_secret_leak(self):
        grant = self.acquire()
        lease_before = self.lease_state()
        events_before = self.event_state()
        secrets = (
            'control-session-one',
            'control-session-two',
            str(grant.lease_token),
            lease_before['owner_session_hash'],
        )

        errors = []
        with mock.patch.object(logging.Logger, '_log') as log_call:
            errors.append(self.assert_control_error(
                'settlement.control.busy',
                self.acquire,
                access=self.second_clerk_access,
                session='control-session-two',
            ))
            errors.append(self.assert_control_error(
                'settlement.control.busy',
                self.acquire,
                session='control-session-two',
            ))

        exposed = repr(errors) + repr(log_call.call_args_list)
        for secret in secrets:
            self.assertNotIn(secret, exposed)
        self.assertEqual(self.lease_state(), lease_before)
        self.assertEqual(self.event_state(), events_before)

    def test_access_validation_has_stable_codes_and_no_partial_writes(self):
        not_activated = self.create_access(
            'NOT-ACTIVATED',
            self.clerk_role,
            access_status=EmployeeAccess.Status.NOT_ACTIVATED,
        )
        disabled_access = self.create_access(
            'DISABLED-ACCESS',
            self.clerk_role,
            access_is_active=False,
        )
        inactive_employee = self.create_access(
            'INACTIVE-EMPLOYEE',
            self.clerk_role,
            employee_status=Employee.Status.DEACTIVATED,
            employee_is_active=False,
        )
        wrong_role = self.create_access('WRONG-ROLE', self.other_role)
        baseline_lease = self.lease_state()
        baseline_events = self.event_state()

        cases = (
            (999999999, 'settlement.control.invalid_access'),
            (True, 'settlement.control.invalid_access'),
            ('1', 'settlement.control.invalid_access'),
            (not_activated.pk, 'settlement.control.inactive_access'),
            (disabled_access.pk, 'settlement.control.inactive_access'),
            (inactive_employee.pk, 'settlement.control.inactive_access'),
            (wrong_role.pk, 'settlement.control.invalid_role'),
        )
        for access_id, code in cases:
            with self.subTest(code=code, access_id=access_id):
                self.assert_control_error(
                    code,
                    acquire_control_lease,
                    owner_access_id=access_id,
                    raw_session_key='validation-session',
                    source='test-validation',
                    now=self.now,
                    ttl=timedelta(minutes=5),
                )
                self.assertEqual(self.lease_state(), baseline_lease)
                self.assertEqual(self.event_state(), baseline_events)

    def test_activated_admin_access_is_allowed(self):
        grant = self.acquire(access=self.admin_access, session='admin-session')

        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.owner_access_id, self.admin_access.pk)
        self.assertEqual(grant.fencing_revision, 1)
        self.assertEqual(
            SettlementControlEvent.objects.get().new_owner_access_id,
            self.admin_access.pk,
        )

    def test_invalid_session_source_and_ttl_fail_without_partial_writes(self):
        baseline_lease = self.lease_state()
        baseline_events = self.event_state()
        cases = (
            ('settlement.control.invalid_session', {'raw_session_key': ''}),
            ('settlement.control.invalid_source', {'source': ''}),
            ('settlement.control.invalid_source', {
                'source': 'prefix-valid-session-suffix',
            }),
            ('settlement.control.invalid_ttl', {'ttl': timedelta(0)}),
        )
        for code, override in cases:
            kwargs = {
                'owner_access_id': self.clerk_access.pk,
                'raw_session_key': 'valid-session',
                'source': 'test-validation',
                'now': self.now,
                'ttl': timedelta(minutes=5),
            }
            kwargs.update(override)
            with self.subTest(code=code):
                self.assert_control_error(code, acquire_control_lease, **kwargs)
                self.assertEqual(self.lease_state(), baseline_lease)
                self.assertEqual(self.event_state(), baseline_events)

    def test_noncanonical_scope_is_rejected_without_creating_second_lease(self):
        before = list(
            SettlementControlLease.objects.values_list('scope', flat=True)
        )

        self.assert_control_error(
            'settlement.control.invalid_scope',
            ensure_control_lease,
            scope='another-settlement-scope',
        )

        self.assertEqual(
            list(SettlementControlLease.objects.values_list('scope', flat=True)),
            before,
        )
        self.assertFalse(SettlementControlEvent.objects.exists())

    def test_heartbeat_updates_only_heartbeat_and_expiry(self):
        grant = self.acquire()
        before = self.lease_state()
        before_events = self.event_state()
        heartbeat_at = self.now + timedelta(minutes=2)

        renewed = heartbeat_control_lease(
            owner_access_id=self.clerk_access.pk,
            raw_session_key='control-session-one',
            lease_token=grant.lease_token,
            fencing_revision=grant.fencing_revision,
            now=heartbeat_at,
            ttl=timedelta(minutes=7),
        )

        after = self.lease_state()
        self.assertEqual(renewed.lease_token, grant.lease_token)
        self.assertEqual(renewed.fencing_revision, grant.fencing_revision)
        self.assertEqual(after['owner_access_id'], before['owner_access_id'])
        self.assertEqual(after['owner_session_hash'], before['owner_session_hash'])
        self.assertEqual(after['lease_token'], before['lease_token'])
        self.assertEqual(after['fencing_revision'], before['fencing_revision'])
        self.assertEqual(after['acquired_at'], before['acquired_at'])
        self.assertEqual(after['heartbeat_at'], heartbeat_at)
        self.assertEqual(after['expires_at'], heartbeat_at + timedelta(minutes=7))
        self.assertEqual(self.event_state(), before_events)

    def test_heartbeat_checks_every_credential_without_partial_writes(self):
        grant = self.acquire()
        baseline_lease = self.lease_state()
        baseline_events = self.event_state()
        cases = (
            ('settlement.control.session_mismatch', {
                'raw_session_key': 'other-session',
            }),
            ('settlement.control.invalid_token', {
                'lease_token': uuid.uuid4(),
            }),
            ('settlement.control.stale_revision', {
                'fencing_revision': grant.fencing_revision + 1,
            }),
            ('settlement.control.busy', {
                'owner_access_id': self.second_clerk_access.pk,
            }),
        )
        for code, override in cases:
            kwargs = {
                'owner_access_id': self.clerk_access.pk,
                'raw_session_key': 'control-session-one',
                'lease_token': grant.lease_token,
                'fencing_revision': grant.fencing_revision,
                'now': self.now + timedelta(minutes=1),
                'ttl': timedelta(minutes=5),
            }
            kwargs.update(override)
            with self.subTest(code=code, override=tuple(override)):
                error = self.assert_control_error(
                    code,
                    heartbeat_control_lease,
                    **kwargs,
                )
                self.assert_error_contains_no_secrets(
                    error,
                    'control-session-one',
                    'other-session',
                    grant.lease_token,
                    baseline_lease['owner_session_hash'],
                    kwargs['lease_token'],
                )
                self.assertEqual(self.lease_state(), baseline_lease)
                self.assertEqual(self.event_state(), baseline_events)

    def test_release_increments_revision_clears_state_and_audits_once(self):
        grant = self.acquire()
        released_at = self.now + timedelta(minutes=1)

        transition = release_control_lease(
            owner_access_id=self.clerk_access.pk,
            raw_session_key='control-session-one',
            lease_token=grant.lease_token,
            fencing_revision=grant.fencing_revision,
            source='test-release',
            reason='Штатное завершение',
            session_metadata={'request_id': 'release-001'},
            now=released_at,
        )

        self.assertEqual(transition.fencing_revision, 2)
        self.assert_fully_free(revision=2)
        events = self.event_state()
        self.assertEqual(
            [event['event_type'] for event in events],
            [
                SettlementControlEvent.EventType.ACQUIRED,
                SettlementControlEvent.EventType.RELEASED,
            ],
        )
        self.assertEqual(events[-1], {
            'event_type': SettlementControlEvent.EventType.RELEASED,
            'actor_access_id': self.clerk_access.pk,
            'previous_owner_access_id': self.clerk_access.pk,
            'new_owner_access_id': None,
            'reason': 'Штатное завершение',
            'source': 'test-release',
            'previous_fencing_revision': 1,
            'new_fencing_revision': 2,
            'session_metadata': {'request_id': 'release-001'},
        })

        before_repeat = self.event_state()
        self.assert_control_error(
            'settlement.control.not_held',
            release_control_lease,
            owner_access_id=self.clerk_access.pk,
            raw_session_key='control-session-one',
            lease_token=grant.lease_token,
            fencing_revision=grant.fencing_revision,
            source='test-repeat-release',
            now=released_at + timedelta(seconds=1),
        )
        self.assert_fully_free(revision=2)
        self.assertEqual(self.event_state(), before_repeat)

    def test_release_checks_every_credential_without_partial_writes(self):
        grant = self.acquire()
        baseline_lease = self.lease_state()
        baseline_events = self.event_state()
        cases = (
            ('settlement.control.busy', {
                'owner_access_id': self.second_clerk_access.pk,
            }),
            ('settlement.control.session_mismatch', {
                'raw_session_key': 'other-session',
            }),
            ('settlement.control.invalid_token', {
                'lease_token': uuid.uuid4(),
            }),
            ('settlement.control.stale_revision', {
                'fencing_revision': grant.fencing_revision + 1,
            }),
            ('settlement.control.invalid_reason', {
                'reason': 'prefix-control-session-one-suffix',
            }),
        )
        for code, override in cases:
            kwargs = {
                'owner_access_id': self.clerk_access.pk,
                'raw_session_key': 'control-session-one',
                'lease_token': grant.lease_token,
                'fencing_revision': grant.fencing_revision,
                'source': 'test-invalid-release',
                'now': self.now + timedelta(minutes=1),
            }
            kwargs.update(override)
            with self.subTest(code=code, override=tuple(override)):
                error = self.assert_control_error(
                    code,
                    release_control_lease,
                    **kwargs,
                )
                self.assert_error_contains_no_secrets(
                    error,
                    'control-session-one',
                    'other-session',
                    grant.lease_token,
                    baseline_lease['owner_session_hash'],
                    kwargs['lease_token'],
                )
                self.assertEqual(self.lease_state(), baseline_lease)
                self.assertEqual(self.event_state(), baseline_events)

    def test_expired_heartbeat_claims_expiry_once_and_returns_expired(self):
        grant = self.acquire(ttl=timedelta(seconds=30))
        expired_at = self.now + timedelta(seconds=30)

        self.assert_control_error(
            'settlement.control.expired',
            heartbeat_control_lease,
            owner_access_id=self.clerk_access.pk,
            raw_session_key='control-session-one',
            lease_token=grant.lease_token,
            fencing_revision=grant.fencing_revision,
            now=expired_at,
            ttl=timedelta(minutes=5),
        )

        self.assert_fully_free(revision=2)
        events = self.event_state()
        self.assertEqual(
            [event['event_type'] for event in events],
            [
                SettlementControlEvent.EventType.ACQUIRED,
                SettlementControlEvent.EventType.EXPIRED,
            ],
        )
        self.assertEqual(events[-1]['actor_access_id'], None)
        self.assertEqual(
            (
                events[-1]['previous_fencing_revision'],
                events[-1]['new_fencing_revision'],
            ),
            (1, 2),
        )

        before_repeat = self.event_state()
        self.assertIsNone(expire_control_lease(
            source='test-expiry-repeat',
            now=expired_at + timedelta(seconds=1),
        ))
        self.assertEqual(self.event_state(), before_repeat)

    def test_expire_command_is_noop_before_deadline_then_expires_atomically(self):
        self.acquire(ttl=timedelta(minutes=2))
        held_state = self.lease_state()
        held_events = self.event_state()

        self.assertIsNone(expire_control_lease(
            source='test-expiry-scan',
            now=self.now + timedelta(minutes=1),
        ))
        self.assertEqual(self.lease_state(), held_state)
        self.assertEqual(self.event_state(), held_events)

        transition = expire_control_lease(
            source='test-expiry-scan',
            session_metadata={'request_id': 'expiry-001'},
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(transition.fencing_revision, 2)
        self.assert_fully_free(revision=2)

    def test_acquire_after_expiry_uses_two_sequential_fencing_transitions(self):
        first = self.acquire(ttl=timedelta(seconds=30))
        second = self.acquire(
            access=self.second_clerk_access,
            session='control-session-two',
            now=self.now + timedelta(seconds=30),
            metadata={'request_id': 'reacquire-001'},
        )

        self.assertNotEqual(second.lease_token, first.lease_token)
        self.assertEqual(second.fencing_revision, 3)
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.owner_access_id, self.second_clerk_access.pk)
        self.assertEqual(lease.fencing_revision, 3)
        events = self.event_state()
        self.assertEqual(
            [event['event_type'] for event in events],
            [
                SettlementControlEvent.EventType.ACQUIRED,
                SettlementControlEvent.EventType.EXPIRED,
                SettlementControlEvent.EventType.ACQUIRED,
            ],
        )
        self.assertEqual(
            [
                (
                    event['previous_fencing_revision'],
                    event['new_fencing_revision'],
                )
                for event in events
            ],
            [(0, 1), (1, 2), (2, 3)],
        )
        self.assertEqual(events[1]['previous_owner_access_id'], self.clerk_access.pk)
        self.assertIsNone(events[1]['new_owner_access_id'])
        self.assertIsNone(events[1]['actor_access_id'])
        self.assertIsNone(events[2]['previous_owner_access_id'])
        self.assertEqual(events[2]['new_owner_access_id'], self.second_clerk_access.pk)

    def test_metadata_uses_allowlist_and_never_persists_secrets(self):
        huge_value = 'x' * 10000
        metadata = {
            'session_kind': 'django',
            'user_agent_hash': 'sha256:user-agent',
            'remote_addr_hash': 'sha256:remote-address',
            'request_id': 'metadata-001-control-session-one',
            'unknown': 'must-not-persist',
            'session_key': 'raw-session-in-metadata',
            'sessionid': 'cookie-session-id',
            'cookie': 'sessionid=cookie-secret',
            'authorization': 'Bearer bearer-secret',
            'lease_token': 'fake-lease-token-secret',
            'password': 'password-secret',
            'secret': 'generic-secret',
        }
        grant = self.acquire(metadata=metadata)
        event_metadata = SettlementControlEvent.objects.get().session_metadata

        self.assertEqual(event_metadata, {
            'session_kind': 'django',
            'user_agent_hash': 'sha256:user-agent',
            'remote_addr_hash': 'sha256:remote-address',
        })
        persisted = json.dumps(
            {
                'lease': self.lease_state(),
                'events': self.event_state(),
            },
            default=str,
            ensure_ascii=False,
        )
        for secret in (
            'control-session-one',
            'raw-session-in-metadata',
            'cookie-session-id',
            'cookie-secret',
            'bearer-secret',
            'fake-lease-token-secret',
            'password-secret',
            'generic-secret',
            'must-not-persist',
        ):
            self.assertNotIn(secret, persisted)
        self.assertIn(str(grant.lease_token), persisted)

        release_control_lease(
            owner_access_id=self.clerk_access.pk,
            raw_session_key='control-session-one',
            lease_token=grant.lease_token,
            fencing_revision=grant.fencing_revision,
            source='test-release',
            now=self.now + timedelta(seconds=1),
        )
        oversized = self.acquire(
            now=self.now + timedelta(seconds=2),
            metadata={'request_id': huge_value},
        )
        latest_metadata = (
            SettlementControlEvent.objects
            .filter(event_type=SettlementControlEvent.EventType.ACQUIRED)
            .order_by('-pk')
            .values_list('session_metadata', flat=True)
            .first()
        )
        self.assertLessEqual(len(latest_metadata.get('request_id', '')), 512)
        self.assertNotEqual(oversized.lease_token, grant.lease_token)

    def test_takeover_requires_active_admin_and_valid_reason_without_writes(self):
        grant = self.acquire()
        before_lease = self.lease_state()
        before_events = self.event_state()
        inactive_admin = self.create_access(
            'INACTIVE-ADMIN',
            self.admin_role,
            access_is_active=False,
        )
        inactive_employee_admin = self.create_access(
            'INACTIVE-EMPLOYEE-ADMIN',
            self.admin_role,
            employee_is_active=False,
        )

        cases = (
            (
                'settlement.control.invalid_role',
                {'access': self.clerk_access},
            ),
            (
                'settlement.control.inactive_access',
                {'access': inactive_admin},
            ),
            (
                'settlement.control.inactive_access',
                {'access': inactive_employee_admin},
            ),
            (
                'settlement.control.takeover_reason_required',
                {'reason': ''},
            ),
            (
                'settlement.control.takeover_reason_required',
                {'reason': '   '},
            ),
            (
                'settlement.control.takeover_reason_required',
                {'reason': 'x' * 513},
            ),
            (
                'settlement.control.takeover_reason_required',
                {'reason': 'control-admin-session'},
            ),
            (
                'settlement.control.takeover_reason_required',
                {'reason': str(grant.lease_token)},
            ),
        )
        for expected_code, kwargs in cases:
            with self.subTest(expected_code=expected_code, kwargs=kwargs):
                error = self.assert_control_error(
                    expected_code,
                    self.takeover,
                    **kwargs,
                )
                self.assert_error_contains_no_secrets(
                    error,
                    'control-admin-session',
                    grant.lease_token,
                )
                self.assertEqual(self.lease_state(), before_lease)
                self.assertEqual(self.event_state(), before_events)

    def test_takeover_replaces_held_owner_and_writes_exact_safe_audit(self):
        previous = self.acquire(session='raw-clerk-session')
        admin_session = 'raw-admin-session-never-persisted'
        reason = '  Approved operational handover  '
        grant = self.takeover(
            session=admin_session,
            reason=reason,
            metadata={
                'session_kind': 'django',
                'request_id': 'takeover-request-001',
                'owner_session_hash': admin_session,
                'lease_token': str(previous.lease_token),
                'nested': {'raw': admin_session},
            },
            now=self.now + timedelta(seconds=1),
        )

        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.owner_access_id, self.admin_access.pk)
        self.assertNotEqual(lease.owner_session_hash, admin_session)
        self.assertEqual(lease.lease_token, grant.lease_token)
        self.assertNotEqual(grant.lease_token, previous.lease_token)
        self.assertEqual(grant.fencing_revision, previous.fencing_revision + 1)
        self.assertEqual(lease.acquired_at, self.now + timedelta(seconds=1))
        self.assertEqual(lease.heartbeat_at, self.now + timedelta(seconds=1))
        self.assertFalse(hasattr(lease, 'reason'))

        events = self.event_state()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1], {
            'event_type': SettlementControlEvent.EventType.TAKEN_OVER,
            'actor_access_id': self.admin_access.pk,
            'previous_owner_access_id': self.clerk_access.pk,
            'new_owner_access_id': self.admin_access.pk,
            'reason': 'Approved operational handover',
            'source': 'test-takeover',
            'previous_fencing_revision': previous.fencing_revision,
            'new_fencing_revision': grant.fencing_revision,
            'session_metadata': {
                'session_kind': 'django',
                'request_id': 'takeover-request-001',
            },
        })
        persisted = json.dumps(
            {
                'lease': self.lease_state(),
                'events': events,
            },
            default=str,
        )
        self.assertNotIn('raw-clerk-session', persisted)
        self.assertNotIn(admin_session, persisted)
        self.assertNotIn(str(previous.lease_token), persisted)

    def test_takeover_invalidates_old_owner_session_token_and_revision(self):
        old = self.acquire(session='old-clerk-session')
        new = self.takeover(
            session='new-admin-session',
            now=self.now + timedelta(seconds=1),
        )

        self.assert_control_error(
            'settlement.control.busy',
            heartbeat_control_lease,
            owner_access_id=self.clerk_access.pk,
            raw_session_key='old-clerk-session',
            lease_token=old.lease_token,
            fencing_revision=old.fencing_revision,
            now=self.now + timedelta(seconds=2),
        )
        self.assert_control_error(
            'settlement.control.session_mismatch',
            heartbeat_control_lease,
            owner_access_id=self.admin_access.pk,
            raw_session_key='old-admin-session',
            lease_token=new.lease_token,
            fencing_revision=new.fencing_revision,
            now=self.now + timedelta(seconds=2),
        )
        self.assert_control_error(
            'settlement.control.invalid_token',
            heartbeat_control_lease,
            owner_access_id=self.admin_access.pk,
            raw_session_key='new-admin-session',
            lease_token=old.lease_token,
            fencing_revision=new.fencing_revision,
            now=self.now + timedelta(seconds=2),
        )
        self.assert_control_error(
            'settlement.control.stale_revision',
            heartbeat_control_lease,
            owner_access_id=self.admin_access.pk,
            raw_session_key='new-admin-session',
            lease_token=new.lease_token,
            fencing_revision=old.fencing_revision,
            now=self.now + timedelta(seconds=2),
        )
        self.assert_control_error(
            'settlement.control.busy',
            release_control_lease,
            owner_access_id=self.clerk_access.pk,
            raw_session_key='old-clerk-session',
            lease_token=old.lease_token,
            fencing_revision=old.fencing_revision,
            source='old-release',
            now=self.now + timedelta(seconds=2),
        )
        self.assert_control_error(
            'settlement.control.invalid_token',
            release_control_lease,
            owner_access_id=self.admin_access.pk,
            raw_session_key='new-admin-session',
            lease_token=old.lease_token,
            fencing_revision=new.fencing_revision,
            source='old-token-release',
            now=self.now + timedelta(seconds=2),
        )
        self.assert_control_error(
            'settlement.control.stale_revision',
            release_control_lease,
            owner_access_id=self.admin_access.pk,
            raw_session_key='new-admin-session',
            lease_token=new.lease_token,
            fencing_revision=old.fencing_revision,
            source='old-revision-release',
            now=self.now + timedelta(seconds=2),
        )
        self.assertEqual(self.lease_state()['lease_token'], new.lease_token)
        self.assertEqual(len(self.event_state()), 2)

    def test_same_admin_session_takeover_is_idempotent_but_validates_reason(self):
        self.acquire()
        first = self.takeover(now=self.now + timedelta(seconds=1))
        events_before = self.event_state()

        second = self.takeover(
            reason='Second confirmed request',
            now=self.now + timedelta(seconds=2),
            ttl=timedelta(minutes=10),
        )

        self.assertEqual(second.lease_token, first.lease_token)
        self.assertEqual(second.fencing_revision, first.fencing_revision)
        self.assertEqual(second.expires_at, self.now + timedelta(minutes=10, seconds=2))
        self.assertEqual(self.event_state(), events_before)
        self.assert_control_error(
            'settlement.control.takeover_reason_required',
            self.takeover,
            reason='   ',
            now=self.now + timedelta(seconds=3),
        )
        self.assertEqual(self.event_state(), events_before)

    def test_takeover_of_free_lease_is_an_acquire_not_taken_over(self):
        grant = self.takeover(reason='Admin starts control')

        self.assertEqual(grant.fencing_revision, 1)
        event = self.event_state()
        self.assertEqual(event, [{
            'event_type': SettlementControlEvent.EventType.ACQUIRED,
            'actor_access_id': self.admin_access.pk,
            'previous_owner_access_id': None,
            'new_owner_access_id': self.admin_access.pk,
            'reason': '',
            'source': 'test-takeover',
            'previous_fencing_revision': 0,
            'new_fencing_revision': 1,
            'session_metadata': {},
        }])

    def test_takeover_of_expired_lease_emits_expired_then_acquired(self):
        previous = self.acquire(ttl=timedelta(seconds=1))

        grant = self.takeover(
            reason='Expired owner replacement',
            now=self.now + timedelta(seconds=2),
        )

        self.assertEqual(grant.fencing_revision, previous.fencing_revision + 2)
        events = self.event_state()
        self.assertEqual(
            [event['event_type'] for event in events],
            [
                SettlementControlEvent.EventType.ACQUIRED,
                SettlementControlEvent.EventType.EXPIRED,
                SettlementControlEvent.EventType.ACQUIRED,
            ],
        )
        self.assertEqual(
            [
                (
                    event['previous_fencing_revision'],
                    event['new_fencing_revision'],
                )
                for event in events
            ],
            [(0, 1), (1, 2), (2, 3)],
        )
        self.assertFalse(any(
            event['event_type'] == SettlementControlEvent.EventType.TAKEN_OVER
            for event in events
        ))
        self.assertTrue(all(event['reason'] == '' for event in events))

    def test_takeover_rolls_back_owner_when_audit_event_cannot_be_created(self):
        self.acquire()
        before_lease = self.lease_state()
        before_events = self.event_state()

        with mock.patch(
            'settlement.control._create_event',
            side_effect=RuntimeError('synthetic audit failure'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'synthetic audit failure'):
                self.takeover(now=self.now + timedelta(seconds=1))

        self.assertEqual(self.lease_state(), before_lease)
        self.assertEqual(self.event_state(), before_events)

    def test_event_is_append_only_through_public_orm_apis(self):
        self.acquire()
        event = SettlementControlEvent.objects.get()

        explicit_instance = SettlementControlEvent(
            event_type=SettlementControlEvent.EventType.ACQUIRED,
            scope='settlement',
            actor_access=self.clerk_access,
            new_owner_access=self.clerk_access,
            source='instance-create',
            previous_fencing_revision=10,
            new_fencing_revision=11,
        )
        explicit_instance.save()
        self.assertIsNotNone(explicit_instance.pk)
        self.assert_control_error(
            'settlement.control.event_immutable',
            explicit_instance.save,
        )

        replacement = SettlementControlEvent(
            pk=event.pk,
            event_type=SettlementControlEvent.EventType.RELEASED,
            scope='settlement',
            source='replacement-instance',
            previous_fencing_revision=20,
            new_fencing_revision=21,
        )
        self.assert_control_error(
            'settlement.control.event_immutable',
            replacement.save,
        )

        event.source = 'mutated-instance'
        self.assert_control_error(
            'settlement.control.event_immutable',
            event.save,
        )
        event.refresh_from_db()
        self.assert_control_error(
            'settlement.control.event_immutable',
            event.delete,
        )
        self.assert_control_error(
            'settlement.control.event_immutable',
            SettlementControlEvent.objects.filter(pk=event.pk).update,
            source='mutated-queryset',
        )
        self.assert_control_error(
            'settlement.control.event_immutable',
            SettlementControlEvent.objects.filter(pk=event.pk).delete,
        )
        self.assert_control_error(
            'settlement.control.event_immutable',
            SettlementControlEvent.objects.bulk_create,
            [SettlementControlEvent(
                event_type=SettlementControlEvent.EventType.ACQUIRED,
                scope='settlement',
                actor_access=self.clerk_access,
                new_owner_access=self.clerk_access,
                source='bulk-create',
                previous_fencing_revision=10,
                new_fencing_revision=11,
            )],
        )
        event.source = 'mutated-bulk-update'
        self.assert_control_error(
            'settlement.control.event_immutable',
            SettlementControlEvent.objects.bulk_update,
            [event],
            ['source'],
        )
        self.assertEqual(SettlementControlEvent.objects.count(), 2)
        event.refresh_from_db()
        self.assertEqual(event.source, 'test-control')


class SettlementControlHttpLifecycleTests(
    SettlementControlFixtureMixin,
    TestCase,
):
    @classmethod
    def setUpTestData(cls):
        cls.create_control_fixtures()
        cls.same_employee_clerk_access = EmployeeAccess.objects.create(
            employee=cls.clerk_access.employee,
            role=cls.clerk_role,
            access_code='CONTROL-ACCESS-SAME-EMPLOYEE',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    def setUp(self):
        SettlementControlLease.objects.filter(scope='settlement').update(
            owner_access=None,
            owner_session_hash='',
            lease_token=None,
            fencing_revision=0,
            acquired_at=None,
            heartbeat_at=None,
            expires_at=None,
        )
        self.acquire_url = reverse('settlement_control_acquire')
        self.heartbeat_url = reverse('settlement_control_heartbeat')
        self.release_url = reverse('settlement_control_release')

    @staticmethod
    def authenticate(client, access):
        session = client.session
        session['employee_access_id'] = access.pk
        session.save()

    @staticmethod
    def set_session_value(client, key, value):
        session = client.session
        session[key] = value
        session.save()

    def assert_no_control_credentials(self, client):
        session = client.session
        for key in control_module.CONTROL_SESSION_CREDENTIAL_KEYS:
            self.assertNotIn(key, session)

    def control_credentials(self, client):
        session = client.session
        return {
            key: session[key]
            for key in control_module.CONTROL_SESSION_CREDENTIAL_KEYS
        }

    def test_get_and_csrf_rejection_do_not_change_lease_or_events(self):
        baseline_lease = self.lease_state()
        baseline_events = self.event_state()

        for url in (self.acquire_url, self.heartbeat_url, self.release_url):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 405)
                self.assertEqual(self.lease_state(), baseline_lease)
                self.assertEqual(self.event_state(), baseline_events)

        csrf_client = Client(enforce_csrf_checks=True)
        self.authenticate(csrf_client, self.clerk_access)
        response = csrf_client.post(self.acquire_url)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(self.lease_state(), baseline_lease)
        self.assertEqual(self.event_state(), baseline_events)

    def test_auto_mutations_are_post_only_and_csrf_protected(self):
        urls = (
            reverse('settlement_auto_preview_create'),
            reverse('settlement_auto_preview_confirm'),
            reverse('settlement_auto_preview_apply'),
            reverse('settlement_auto_preview_apply_night'),
            reverse('settlement_auto_preview_apply_day'),
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 405)

        csrf_client = Client(enforce_csrf_checks=True)
        self.authenticate(csrf_client, self.clerk_access)
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(
                    csrf_client.post(url, data='{}', content_type='application/json').status_code,
                    403,
                )

    def test_auto_state_has_safe_empty_state_without_approved_cohort(self):
        self.authenticate(self.client, self.clerk_access)

        response = self.client.get(reverse('settlement_auto_state'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'ok': True,
            'state': 'no_cohort',
            'cohorts': [],
            'preview': None,
        })
        rendered = json.dumps(response.json())
        for forbidden in (
            'lease_token', 'fencing_revision', 'owner_access_id',
            'raw_session_key', 'session_binding', 'employee_access_id',
        ):
            self.assertNotIn(forbidden, rendered)

    def test_auto_create_uses_exact_session_context_and_ignores_post_credentials(self):
        self.authenticate(self.client, self.clerk_access)
        self.assertEqual(self.client.post(self.acquire_url).status_code, 200)
        secret = str(uuid.uuid4())
        fake_run = mock.sentinel.auto_run
        response_payload = {'id': 41, 'status': 'draft', 'placements': [], 'unresolved': []}

        with (
            mock.patch('settlement.views.create_settlement_preview_run', return_value=fake_run) as create,
            mock.patch('settlement.views._auto_settlement_run_payload', return_value=response_payload),
        ):
            response = self.client.post(
                reverse('settlement_auto_preview_create'),
                data=json.dumps({
                    'cohort_id': 17,
                    'owner_access_id': self.second_clerk_access.pk,
                    'lease_token': secret,
                    'fencing_revision': 999,
                    'raw_session_key': 'spoofed-session',
                }),
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['preview'], response_payload)
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs['cohort_id'], 17)
        context = kwargs['control_context']
        self.assertEqual(context.owner_access_id, self.clerk_access.pk)
        self.assertEqual(context.raw_session_key, self.client.session.session_key)
        self.assertNotEqual(str(context.lease_token), secret)
        response_text = response.content.decode()
        self.assertNotIn(secret, response_text)
        self.assertNotIn(str(context.lease_token), response_text)
        self.assertNotIn(context.raw_session_key, response_text)

    def test_auto_confirm_forwards_run_and_legacy_apply_is_controlled_409(self):
        self.authenticate(self.client, self.clerk_access)
        self.assertEqual(self.client.post(self.acquire_url).status_code, 200)
        fake_run = mock.sentinel.auto_run
        response_payload = {'id': 51, 'status': 'confirmed', 'application': None}

        with (
            mock.patch('settlement.views.confirm_settlement_preview_run', return_value=fake_run) as confirm,
            mock.patch('settlement.views._auto_settlement_run_payload', return_value=response_payload),
        ):
            confirmed = self.client.post(
                reverse('settlement_auto_preview_confirm'),
                data=json.dumps({'run_id': 51, 'lease_token': 'ignored'}),
                content_type='application/json',
            )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirm.call_args.kwargs['run_id'], 51)
        self.assertEqual(
            confirm.call_args.kwargs['control_context'].owner_access_id,
            self.clerk_access.pk,
        )

        secret = str(uuid.uuid4())
        applied = self.client.post(
            reverse('settlement_auto_preview_apply') + '?work_shift=night&revision=777',
            data=json.dumps({
                'run_id': 51,
                'work_shift': 'day',
                'apply_date': '2099-01-01',
                'owner_access_id': self.second_clerk_access.pk,
                'employee_access_id': self.second_clerk_access.pk,
                'lease_token': secret,
                'fencing_revision': 999,
                'raw_session_key': 'spoofed-session',
                'session_binding': 'spoofed-binding',
            }),
            content_type='application/json',
        )
        self.assertEqual(applied.status_code, 409)
        self.assertEqual(applied.json(), {
            'ok': False,
            'error': 'План необходимо применить отдельно для ночной и дневной смены.',
            'code': 'settlement.apply.shift_split_required',
            'details': {},
        })
        self.assertNotIn(secret, applied.content.decode())
        self.assertFalse(SettlementPreviewApplication.objects.exists())
        self.assertFalse(SettlementPreviewApplicationItem.objects.exists())
        self.assertFalse(EmployeeBedOccupancy._base_manager.exists())

    def test_shift_apply_endpoints_use_fixed_shift_and_server_control_context(self):
        self.authenticate(self.client, self.clerk_access)
        self.assertEqual(self.client.post(self.acquire_url).status_code, 200)
        secret = str(uuid.uuid4())
        fake_application = mock.Mock(preview_run_id=51)
        fake_run = mock.sentinel.shift_run
        response_payload = {
            'id': 51,
            'status': 'confirmed',
            'shift_apply': {},
        }

        for route_name, expected_shift, spoofed_shift in (
            ('settlement_auto_preview_apply_night', 'night', 'day'),
            ('settlement_auto_preview_apply_day', 'day', 'night'),
        ):
            queryset = mock.Mock()
            queryset.get.return_value = fake_run
            with (
                mock.patch(
                    'settlement.views.apply_confirmed_settlement_preview',
                    return_value=fake_application,
                ) as apply,
                mock.patch(
                    'settlement.views._auto_settlement_run_queryset',
                    return_value=queryset,
                ),
                mock.patch(
                    'settlement.views._auto_settlement_run_payload',
                    return_value=response_payload,
                ),
            ):
                response = self.client.post(
                    reverse(route_name) + '?work_shift=' + spoofed_shift + '&now=2099-01-01',
                    data=json.dumps({
                        'run_id': 51,
                        'work_shift': spoofed_shift,
                        'apply_date': '2099-01-01',
                        'now': '2099-01-01T00:00:00Z',
                        'owner_access_id': self.second_clerk_access.pk,
                        'employee_access_id': self.second_clerk_access.pk,
                        'lease_token': secret,
                        'fencing_revision': 999,
                        'raw_session_key': 'spoofed-session',
                        'session_binding': 'spoofed-binding',
                    }),
                    content_type='application/json',
                )

            self.assertEqual(response.status_code, 200, response.content)
            kwargs = apply.call_args.kwargs
            self.assertEqual(kwargs['run_id'], 51)
            self.assertEqual(kwargs['work_shift'], expected_shift)
            self.assertNotIn('now', kwargs)
            context = kwargs['control_context']
            self.assertEqual(context.owner_access_id, self.clerk_access.pk)
            self.assertEqual(context.raw_session_key, self.client.session.session_key)
            self.assertNotEqual(str(context.lease_token), secret)
            rendered = response.content.decode()
            self.assertNotIn(secret, rendered)
            self.assertNotIn(context.raw_session_key, rendered)

    def test_auto_confirmation_conflicts_are_controlled(self):
        self.authenticate(self.client, self.clerk_access)
        self.assertEqual(self.client.post(self.acquire_url).status_code, 200)
        for code in (
            'settlement.preview.stale_source',
            'settlement.preview.concurrent_confirmation',
        ):
            with self.subTest(code=code), mock.patch(
                'settlement.views.confirm_settlement_preview_run',
                side_effect=ValidationError('Контролируемый конфликт.', code=code),
            ):
                response = self.client.post(
                    reverse('settlement_auto_preview_confirm'),
                    data=json.dumps({'run_id': 51}),
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()['code'], code)

    def test_auto_mutations_require_held_control_and_report_controlled_loss(self):
        self.authenticate(self.client, self.clerk_access)

        response = self.client.post(
            reverse('settlement_auto_preview_create'),
            data=json.dumps({'cohort_id': 1}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'settlement.control.not_held')

    def test_legacy_auto_apply_requires_held_control_before_shift_split_response(self):
        self.authenticate(self.client, self.clerk_access)
        apply_routes = (
            'settlement_auto_preview_apply',
            'settlement_auto_preview_apply_night',
            'settlement_auto_preview_apply_day',
        )
        for route_name in apply_routes:
            with self.subTest(route_name=route_name, control='not_held'):
                response = self.client.post(
                    reverse(route_name),
                    data=json.dumps({'run_id': 8, 'work_shift': 'night'}),
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()['code'], 'settlement.control.not_held')

        self.assertEqual(self.client.post(self.acquire_url).status_code, 200)
        self.assertEqual(self.client.post(self.release_url).status_code, 200)
        for route_name in apply_routes:
            with self.subTest(route_name=route_name, control='lost'):
                lost = self.client.post(
                    reverse(route_name),
                    data=json.dumps({'run_id': 8, 'work_shift': 'day'}),
                    content_type='application/json',
                )
                self.assertEqual(lost.status_code, 409)
                self.assertEqual(lost.json()['code'], 'settlement.control.not_held')
        self.assertFalse(SettlementPreviewApplication.objects.exists())
        self.assertFalse(SettlementPreviewApplicationItem.objects.exists())
        self.assertFalse(EmployeeBedOccupancy._base_manager.exists())

    def test_acquire_uses_exact_session_access_and_stores_only_server_credentials(self):
        self.authenticate(self.client, self.clerk_access)
        response = self.client.post(
            self.acquire_url,
            data=json.dumps({
                'owner_access_id': self.second_clerk_access.pk,
                'lease_token': str(uuid.uuid4()),
                'fencing_revision': 999,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {'ok', 'status', 'expires_at'})
        self.assertEqual(response.json()['status'], 'held')
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.owner_access_id, self.clerk_access.pk)
        credentials = self.control_credentials(self.client)
        self.assertEqual(
            credentials[control_module.CONTROL_SESSION_OWNER_ACCESS_ID_KEY],
            self.clerk_access.pk,
        )
        self.assertEqual(
            credentials[control_module.CONTROL_SESSION_LEASE_TOKEN_KEY],
            str(lease.lease_token),
        )
        self.assertEqual(
            credentials[control_module.CONTROL_SESSION_FENCING_REVISION_KEY],
            lease.fencing_revision,
        )
        response_text = response.content.decode()
        self.assertNotIn(str(lease.lease_token), response_text)
        self.assertNotIn(self.client.session.session_key, response_text)
        self.assertNotEqual(lease.owner_session_hash, self.client.session.session_key)
        self.assertEqual(
            list(SettlementControlEvent.objects.values_list('event_type', flat=True)),
            [SettlementControlEvent.EventType.ACQUIRED],
        )

    def test_inactive_exact_access_is_not_replaced_by_second_access_of_employee(self):
        self.authenticate(self.client, self.clerk_access)
        EmployeeAccess.objects.filter(pk=self.clerk_access.pk).update(is_active=False)

        response = self.client.post(self.acquire_url)

        self.assertEqual(response.status_code, 409)
        self.assertContains(
            response,
            'Роль неактивна — доступен только просмотр',
            status_code=409,
        )
        self.assertIsNone(SettlementControlLease.objects.get().owner_access_id)
        self.assertFalse(SettlementControlEvent.objects.exists())
        self.assert_no_control_credentials(self.client)

    def test_other_clerk_gets_busy_without_credentials(self):
        self.authenticate(self.client, self.clerk_access)
        self.assertEqual(self.client.post(self.acquire_url).status_code, 200)
        other_client = Client()
        self.authenticate(other_client, self.second_clerk_access)

        response = other_client.post(self.acquire_url)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'settlement.control.busy')
        self.assertEqual(response.json()['status'], 'busy')
        self.assert_no_control_credentials(other_client)
        self.assertEqual(SettlementControlLease.objects.get().owner_access_id, self.clerk_access.pk)
        self.assertEqual(SettlementControlEvent.objects.count(), 1)

    def test_owner_heartbeat_renews_without_new_event_or_secret_response(self):
        self.authenticate(self.client, self.clerk_access)
        self.client.post(self.acquire_url)
        lease = SettlementControlLease.objects.get()
        previous_expiry = lease.expires_at
        SettlementControlLease.objects.filter(pk=lease.pk).update(
            expires_at=timezone.now() + timedelta(seconds=10),
        )

        response = self.client.post(self.heartbeat_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {'ok', 'status', 'expires_at'})
        lease.refresh_from_db()
        self.assertGreater(lease.expires_at, previous_expiry)
        self.assertEqual(SettlementControlEvent.objects.count(), 1)
        self.assertNotIn(str(lease.lease_token), response.content.decode())

    def test_other_session_cannot_use_copied_server_credentials(self):
        self.authenticate(self.client, self.clerk_access)
        self.client.post(self.acquire_url)
        baseline_lease = self.lease_state()
        credentials = self.control_credentials(self.client)
        other_session = Client()
        self.authenticate(other_session, self.clerk_access)
        session = other_session.session
        session.update(credentials)
        session.save()

        response = other_session.post(self.heartbeat_url)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'settlement.control.session_mismatch')
        self.assertEqual(self.lease_state(), baseline_lease)
        self.assert_no_control_credentials(other_session)

    def test_heartbeat_rejects_stale_revision_and_clears_credentials(self):
        self.authenticate(self.client, self.clerk_access)
        self.client.post(self.acquire_url)
        baseline_lease = self.lease_state()
        key = control_module.CONTROL_SESSION_FENCING_REVISION_KEY
        self.set_session_value(self.client, key, baseline_lease['fencing_revision'] + 1)

        response = self.client.post(self.heartbeat_url)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'settlement.control.stale_revision')
        self.assertEqual(self.lease_state(), baseline_lease)
        self.assert_no_control_credentials(self.client)

    def test_heartbeat_rejects_invalid_token_and_clears_credentials(self):
        self.authenticate(self.client, self.clerk_access)
        self.client.post(self.acquire_url)
        baseline_lease = self.lease_state()
        self.set_session_value(
            self.client,
            control_module.CONTROL_SESSION_LEASE_TOKEN_KEY,
            str(uuid.uuid4()),
        )

        response = self.client.post(self.heartbeat_url)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'settlement.control.invalid_token')
        self.assertEqual(self.lease_state(), baseline_lease)
        self.assert_no_control_credentials(self.client)

    def test_heartbeat_expires_lease_with_controlled_result_and_clears_credentials(self):
        self.authenticate(self.client, self.clerk_access)
        self.client.post(self.acquire_url)
        expired_at = timezone.now() - timedelta(seconds=1)
        SettlementControlLease.objects.update(
            acquired_at=expired_at - timedelta(minutes=2),
            heartbeat_at=expired_at - timedelta(minutes=1),
            expires_at=expired_at,
        )

        response = self.client.post(self.heartbeat_url)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'settlement.control.expired')
        self.assertEqual(response.json()['status'], 'free')
        lease = SettlementControlLease.objects.get()
        self.assertIsNone(lease.owner_access_id)
        self.assertIsNone(lease.lease_token)
        self.assert_no_control_credentials(self.client)
        self.assertEqual(
            list(SettlementControlEvent.objects.order_by('pk').values_list('event_type', flat=True)),
            [
                SettlementControlEvent.EventType.ACQUIRED,
                SettlementControlEvent.EventType.EXPIRED,
            ],
        )

    def test_owner_release_frees_lease_clears_credentials_and_repeat_is_safe(self):
        self.authenticate(self.client, self.clerk_access)
        self.client.post(self.acquire_url)

        response = self.client.post(self.release_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {'ok', 'status', 'occurred_at'})
        self.assertEqual(response.json()['status'], 'free')
        lease = SettlementControlLease.objects.get()
        self.assertIsNone(lease.owner_access_id)
        self.assertIsNone(lease.lease_token)
        self.assert_no_control_credentials(self.client)
        self.assertEqual(
            list(SettlementControlEvent.objects.order_by('pk').values_list('event_type', flat=True)),
            [
                SettlementControlEvent.EventType.ACQUIRED,
                SettlementControlEvent.EventType.RELEASED,
            ],
        )

        repeated = self.client.post(self.release_url)
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(repeated.json()['code'], 'settlement.control.not_held')
        self.assertEqual(SettlementControlEvent.objects.count(), 2)

    def test_foreign_release_cannot_free_owner_lease(self):
        self.authenticate(self.client, self.clerk_access)
        self.client.post(self.acquire_url)
        baseline_lease = self.lease_state()
        credentials = self.control_credentials(self.client)
        foreign_client = Client()
        self.authenticate(foreign_client, self.second_clerk_access)
        foreign_session = foreign_client.session
        foreign_session.update(credentials)
        foreign_session.save()

        response = foreign_client.post(self.release_url)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'settlement.control.session_mismatch')
        self.assertEqual(self.lease_state(), baseline_lease)
        self.assertEqual(SettlementControlEvent.objects.count(), 1)
        self.assert_no_control_credentials(foreign_client)

    def test_event_failure_after_lease_lock_rolls_back_lease_and_session(self):
        self.authenticate(self.client, self.clerk_access)
        baseline_lease = self.lease_state()
        baseline_events = self.event_state()
        forced_error = ValidationError(
            'Контрольная ошибка аудита.',
            code='settlement.control.audit_failed',
        )

        with mock.patch.object(control_module, '_create_event', side_effect=forced_error):
            response = self.client.post(self.acquire_url)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'settlement.control.audit_failed')
        self.assertEqual(self.lease_state(), baseline_lease)
        self.assertEqual(self.event_state(), baseline_events)
        self.assert_no_control_credentials(self.client)

    def test_map_renders_read_only_control_panel_without_exposing_credentials(self):
        self.authenticate(self.client, self.clerk_access)
        session = self.client.session
        secret_token = '00000000-0000-4000-8000-000000000123'
        secret_revision = 987654321
        session[control_module.CONTROL_SESSION_OWNER_ACCESS_ID_KEY] = self.clerk_access.pk
        session[control_module.CONTROL_SESSION_LEASE_TOKEN_KEY] = secret_token
        session[control_module.CONTROL_SESSION_FENCING_REVISION_KEY] = secret_revision
        session.save()
        baseline_lease = self.lease_state()
        baseline_events = self.event_state()

        response = self.client.get(reverse('settlement_map'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-control-panel')
        self.assertContains(response, 'data-control-state="free"')
        self.assertContains(response, 'data-control-acquire disabled')
        self.assertContains(response, 'data-control-release disabled')
        self.assertContains(response, self.acquire_url)
        self.assertContains(response, self.heartbeat_url)
        self.assertContains(response, self.release_url)
        self.assertEqual(response.content.count(b'-settlement-map-v32'), 2)
        self.assertNotContains(response, '-settlement-map-v31')
        self.assertContains(response, 'data-auto-apply-night-url')
        self.assertContains(response, 'data-auto-apply-day-url')
        self.assertContains(response, 'Применить ночную смену')
        self.assertContains(response, 'Применить дневную смену')
        self.assertNotContains(response, 'data-auto-apply-url')
        response_text = response.content.decode()
        self.assertNotIn(secret_token, response_text)
        self.assertNotIn(str(secret_revision), response_text)
        self.assertNotIn(self.client.session.session_key, response_text)
        self.assertNotIn('settlement_control_lease_token', response_text)
        self.assertNotIn('settlement_control_fencing_revision', response_text)
        self.assertNotIn('settlement_control_owner_access_id', response_text)
        self.assertEqual(self.lease_state(), baseline_lease)
        self.assertEqual(self.event_state(), baseline_events)

    def test_map_script_uses_explicit_nonparallel_lifecycle_without_client_secrets(self):
        script_path = finders.find('js/settlement-clerk.js')
        self.assertIsNotNone(script_path)
        with open(script_path, encoding='utf-8') as script_file:
            script = script_file.read()

        for required_fragment in (
            'function initializeControlLifecycle()',
            'function acquireControl()',
            'function heartbeatControl()',
            'function releaseControl()',
            'heartbeatRetryDelays = [2500, 6000]',
            'if (heartbeatInFlight || controlActionInFlight) return;',
            'if (controlHeld || controlActionInFlight || heartbeatInFlight || controlAccessDenied) return;',
            'if (!controlHeld)',
            'data-bed-drag-handle',
            'Управление не начато',
            'Вы управляете расселением',
            'Управление занято другим сотрудником',
            'Связь с управлением потеряна',
            'function loadAutoState(cohortId, retryCount)',
            'function runAutoMutation(url, payload)',
            'function applyAutoPreviewShift(workShift, confirmReplaceManual)',
            'root.dataset.autoApplyNightUrl',
            'root.dataset.autoApplyDayUrl',
            'manual_replacement_confirmation_required',
            'if (autoMutationInFlight || !controlHeld) return Promise.resolve(null);',
        ):
            self.assertIn(required_fragment, script)
        for forbidden_fragment in (
            'beforeunload',
            'settlement_control_lease_token',
            'settlement_control_fencing_revision',
            'settlement_control_owner_access_id',
            'owner_session_hash',
            'data-auto-preview-form',
            'root.dataset.autoApplyUrl',
        ):
            self.assertNotIn(forbidden_fragment, script)
        initialization = script.split(
            'function initializeControlLifecycle()',
            1,
        )[1].split('function updateMapAfterSettlement', 1)[0]
        self.assertIn('heartbeatControl();', initialization)
        self.assertNotIn('acquireControl();', initialization)

    def test_writer_rejects_session_access_switch_and_copied_credentials(self):
        self.authenticate(self.client, self.clerk_access)
        self.assertEqual(self.client.post(self.acquire_url).status_code, 200)
        credentials = self.control_credentials(self.client)
        session = self.client.session
        session['employee_access_id'] = self.same_employee_clerk_access.pk
        session.save()

        switched = self.client.post(
            reverse('settlement_occupancy_create'),
            data={'action': 'release', 'bed_stable_id': 'CONTROL-NOT-USED'},
            content_type='application/json',
        )

        self.assertEqual(switched.status_code, 409)
        self.assertEqual(
            switched.json()['code'],
            'settlement.control.session_mismatch',
        )
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

        copied_client = Client()
        self.authenticate(copied_client, self.clerk_access)
        copied_session = copied_client.session
        copied_session.update(credentials)
        copied_session.save()
        copied = copied_client.post(
            reverse('settlement_occupancy_create'),
            data={'action': 'release', 'bed_stable_id': 'CONTROL-NOT-USED'},
            content_type='application/json',
        )

        self.assertEqual(copied.status_code, 409)
        self.assertEqual(
            copied.json()['code'],
            'settlement.control.session_mismatch',
        )
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_no_takeover_endpoint_was_added(self):
        with self.assertRaises(Resolver404):
            resolve('/clerk/settlement/control/takeover/')


class SettlementControlledWriterTests(
    SettlementControlFixtureMixin,
    TestCase,
):
    @classmethod
    def setUpTestData(cls):
        cls.create_control_fixtures()
        cls.watch_composition = WatchComposition.objects.create(
            code='control-writer-composition',
            name='Control writer composition',
            is_active=True,
        )
        today = timezone.localdate()
        cls.watch_period = WatchPeriod.objects.create(
            name='Control writer period',
            watch_composition=cls.watch_composition,
            starts_on=today,
            ends_on=today + timedelta(days=14),
            is_active=True,
        )
        cls.same_employee_access = EmployeeAccess.objects.create(
            employee=cls.clerk_access.employee,
            role=cls.clerk_role,
            access_code='CONTROL-WRITER-SAME-EMPLOYEE',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.subject = Employee.objects.create(
            full_name='Control writer subject',
            personnel_number='CONTROL-WRITER-SUBJECT',
            sex=Employee.Sex.MALE,
            watch_composition=cls.watch_composition,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.subject_resident = SettlementResident.objects.create(
            resident_type=SettlementResident.ResidentType.EMPLOYEE,
            employee=cls.subject,
            status=SettlementResident.Status.ACTIVE,
            external_sex=None,
            created_by_access=cls.clerk_access,
        )
        cls.unrelated_subject = Employee.objects.create(
            full_name='Control writer unrelated subject',
            personnel_number='CONTROL-WRITER-UNRELATED',
            sex=Employee.Sex.MALE,
            watch_composition=cls.watch_composition,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.unrelated_resident = SettlementResident.objects.create(
            resident_type=SettlementResident.ResidentType.EMPLOYEE,
            employee=cls.unrelated_subject,
            status=SettlementResident.Status.ACTIVE,
            external_sex=None,
            created_by_access=cls.clerk_access,
        )
        cls.manual_context = _create_manual_shift_application(
            resident=cls.subject_resident,
            period=cls.watch_period,
            actor=cls.clerk_access.employee,
            access=cls.clerk_access,
            suffix='control-writer',
            additional_residents=(cls.unrelated_resident,),
        )
        cls.dormitory = Dormitory.objects.create(number='CONTROL-WRITER')
        cls.first_room = PhysicalRoom.objects.create(
            dormitory=cls.dormitory,
            floor=1,
            number=1,
            room_type=PhysicalRoom.RoomType.STANDARD,
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            capacity=1,
            corridor_side=PhysicalRoom.CorridorSide.LEFT,
            side_position=1,
        )
        cls.second_room = PhysicalRoom.objects.create(
            dormitory=cls.dormitory,
            floor=1,
            number=2,
            room_type=PhysicalRoom.RoomType.STANDARD,
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            capacity=1,
            corridor_side=PhysicalRoom.CorridorSide.RIGHT,
            side_position=1,
        )
        cls.first_bed = PhysicalBed.objects.create(
            room=cls.first_room,
            stable_id='CONTROL-WRITER-A1',
            block=PhysicalBed.Block.A,
            position=1,
        )
        cls.second_bed = PhysicalBed.objects.create(
            room=cls.second_room,
            stable_id='CONTROL-WRITER-B1',
            block=PhysicalBed.Block.B,
            position=1,
        )

    def setUp(self):
        SettlementControlLease.objects.filter(scope='settlement').update(
            owner_access=None,
            owner_session_hash='',
            lease_token=None,
            fencing_revision=0,
            acquired_at=None,
            heartbeat_at=None,
            expires_at=None,
        )
        self.raw_session_key = 'controlled-writer-session'

    def write_context(self, access=None):
        access = access or self.clerk_access
        grant = acquire_control_lease(
            owner_access_id=access.pk,
            raw_session_key=self.raw_session_key,
            source='controlled-writer-test',
        )
        return SettlementControlWriteContext(
            owner_access_id=access.pk,
            raw_session_key=self.raw_session_key,
            lease_token=str(grant.lease_token),
            fencing_revision=grant.fencing_revision,
        )

    @staticmethod
    def error_code(error):
        return error.exception.error_list[0].code

    def settle(self, *, control_context):
        return settle_employee_on_bed(
            bed_stable_id=self.first_bed.stable_id,
            employee_id=self.subject.pk,
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            control_context=control_context,
        )

    def create_existing_occupancy(self):
        return EmployeeBedOccupancy.objects.create(
            resident=self.subject_resident,
            physical_bed=self.first_bed,
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_at=timezone.now() - timedelta(minutes=5),
            starts_at=timezone.now() - timedelta(minutes=5),
            settled_by=self.clerk_access.employee,
        )

    def test_write_context_is_frozen_and_contains_only_server_credentials(self):
        context = self.write_context()

        self.assertEqual(
            set(context.__dataclass_fields__),
            {
                'owner_access_id',
                'raw_session_key',
                'lease_token',
                'fencing_revision',
            },
        )
        self.assertNotIn(context.raw_session_key, repr(context))
        self.assertNotIn(context.lease_token, repr(context))
        with self.assertRaises(FrozenInstanceError):
            context.fencing_revision = 999

    def test_all_public_writers_fail_closed_without_context(self):
        operations = (
            lambda: self.settle(control_context=None),
            lambda: relocate_employee_to_bed(
                bed_stable_id=self.second_bed.stable_id,
                occupancy_id=1,
                control_context=None,
            ),
            lambda: release_employee_from_bed(
                occupancy_id=1,
                control_context=None,
            ),
        )

        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(ValidationError) as error:
                    operation()
                self.assertEqual(
                    self.error_code(error),
                    'settlement.control.not_held',
                )
                self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_exact_owner_employee_is_settled_by_and_unrelated_access_is_not_planned(self):
        context = self.write_context()
        plans = []
        resident_plans = []
        original_builder = control_module.build_employee_access_lock_plan
        original_resident_builder = settlement_services.build_settlement_resident_lock_plan

        def observed_builder(**kwargs):
            plan = original_builder(**kwargs)
            plans.append(plan)
            return plan

        def observed_resident_builder(**kwargs):
            plan = original_resident_builder(**kwargs)
            resident_plans.append(plan)
            return plan

        with (
            mock.patch.object(
                control_module,
                'build_employee_access_lock_plan',
                side_effect=observed_builder,
            ),
            mock.patch.object(
                settlement_services,
                'build_settlement_resident_lock_plan',
                side_effect=observed_resident_builder,
            ),
        ):
            occupancy = self.settle(control_context=context)

        self.assertEqual(occupancy.settled_by_id, self.clerk_access.employee_id)
        self.assertEqual(occupancy.resident_id, self.subject_resident.pk)
        self.assertEqual(plans[-1].access_ids, (self.clerk_access.pk,))
        self.assertNotIn(self.same_employee_access.pk, plans[-1].access_ids)
        self.assertEqual(
            plans[-1].employee_ids,
            tuple(sorted({self.clerk_access.employee_id, self.subject.pk})),
        )
        self.assertEqual(resident_plans[-1].resident_ids, (self.subject_resident.pk,))
        self.assertEqual(resident_plans[-1].employee_ids, (self.subject.pk,))
        self.assertNotIn(self.unrelated_resident.pk, resident_plans[-1].resident_ids)

    def test_employee_adapter_requires_exact_existing_resident_without_fallback(self):
        context = self.write_context()
        employee_without_resident = Employee.objects.create(
            full_name='Control writer missing resident',
            personnel_number='CONTROL-WRITER-MISSING-RESIDENT',
            sex=Employee.Sex.MALE,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )

        with self.assertRaises(ValidationError) as error:
            settle_employee_on_bed(
                bed_stable_id=self.first_bed.stable_id,
                employee_id=employee_without_resident.pk,
                assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
                control_context=context,
            )

        self.assertEqual(self.error_code(error), 'settlement_resident_not_found')
        self.assertFalse(EmployeeBedOccupancy.objects.exists())
        self.assertFalse(
            SettlementResident.objects.filter(employee=employee_without_resident).exists(),
        )

    def test_archived_or_inactive_subject_is_rejected_without_write(self):
        context = self.write_context()
        SettlementResident._base_manager.filter(pk=self.subject_resident.pk).update(
            status=SettlementResident.Status.ARCHIVED,
            archived_at=timezone.now(),
        )
        with self.assertRaises(ValidationError) as archived_error:
            self.settle(control_context=context)
        self.assertEqual(self.error_code(archived_error), 'settlement_resident_inactive')
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

        SettlementResident._base_manager.filter(pk=self.subject_resident.pk).update(
            status=SettlementResident.Status.ACTIVE,
            archived_at=None,
        )
        Employee.objects.filter(pk=self.subject.pk).update(is_active=False)
        with self.assertRaises(ValidationError) as employee_error:
            self.settle(control_context=context)
        self.assertEqual(self.error_code(employee_error), 'settlement_employee_inactive')
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_stale_resident_plan_is_rejected_and_rolled_back(self):
        context = self.write_context()
        original_builder = settlement_services.build_settlement_resident_lock_plan

        def mutate_resident_after_preread(**kwargs):
            plan = original_builder(**kwargs)
            SettlementResident._base_manager.filter(pk=self.subject_resident.pk).update(
                revision=self.subject_resident.revision + 1,
            )
            return plan

        with mock.patch.object(
            settlement_services,
            'build_settlement_resident_lock_plan',
            side_effect=mutate_resident_after_preread,
        ):
            with self.assertRaises(ValidationError) as error:
                self.settle(control_context=context)

        self.assertEqual(self.error_code(error), 'settlement.resident.stale_subject')
        self.subject_resident.refresh_from_db()
        self.assertEqual(self.subject_resident.revision, 1)
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_inactive_exact_access_is_not_replaced_by_second_access(self):
        context = self.write_context()
        EmployeeAccess.objects.filter(pk=self.clerk_access.pk).update(is_active=False)

        with self.assertRaises(ValidationError) as error:
            self.settle(control_context=context)

        self.assertEqual(
            self.error_code(error),
            'settlement.control.inactive_access',
        )
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

        EmployeeAccess.objects.filter(pk=self.clerk_access.pk).update(
            is_active=True,
            status=EmployeeAccess.Status.DEACTIVATED,
        )
        with self.assertRaises(ValidationError) as status_error:
            self.settle(control_context=context)
        self.assertEqual(
            self.error_code(status_error),
            'settlement.control.inactive_access',
        )
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_wrong_session_token_revision_and_expiry_never_write(self):
        context = self.write_context()
        invalid_contexts = (
            SettlementControlWriteContext(
                owner_access_id=context.owner_access_id,
                raw_session_key='different-controlled-writer-session',
                lease_token=context.lease_token,
                fencing_revision=context.fencing_revision,
            ),
            SettlementControlWriteContext(
                owner_access_id=context.owner_access_id,
                raw_session_key=context.raw_session_key,
                lease_token=str(uuid.uuid4()),
                fencing_revision=context.fencing_revision,
            ),
            SettlementControlWriteContext(
                owner_access_id=context.owner_access_id,
                raw_session_key=context.raw_session_key,
                lease_token=context.lease_token,
                fencing_revision=context.fencing_revision + 1,
            ),
        )
        expected_codes = (
            'settlement.control.session_mismatch',
            'settlement.control.invalid_token',
            'settlement.control.stale_revision',
        )

        for invalid_context, expected_code in zip(invalid_contexts, expected_codes):
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(ValidationError) as error:
                    self.settle(control_context=invalid_context)
                self.assertEqual(self.error_code(error), expected_code)
                self.assertFalse(EmployeeBedOccupancy.objects.exists())

        expired_at = timezone.now() - timedelta(seconds=1)
        SettlementControlLease.objects.update(
            acquired_at=expired_at - timedelta(minutes=2),
            heartbeat_at=expired_at - timedelta(minutes=1),
            expires_at=expired_at,
        )
        with self.assertRaises(ValidationError) as error:
            self.settle(control_context=context)
        self.assertEqual(self.error_code(error), 'settlement.control.expired')
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_inactive_owner_employee_and_role_fail_without_write(self):
        context = self.write_context()
        Employee.objects.filter(pk=self.clerk_access.employee_id).update(is_active=False)
        with self.assertRaises(ValidationError) as employee_error:
            self.settle(control_context=context)
        self.assertEqual(
            self.error_code(employee_error),
            'settlement.control.inactive_access',
        )
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

        Employee.objects.filter(pk=self.clerk_access.employee_id).update(
            is_active=True,
            status=Employee.Status.DEACTIVATED,
        )
        with self.assertRaises(ValidationError) as status_error:
            self.settle(control_context=context)
        self.assertEqual(
            self.error_code(status_error),
            'settlement.control.inactive_access',
        )
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

        Employee.objects.filter(pk=self.clerk_access.employee_id).update(
            status=Employee.Status.ACTIVE,
        )
        Role.objects.filter(pk=self.clerk_role.pk).update(is_active=False)
        with self.assertRaises(ValidationError) as role_error:
            self.settle(control_context=context)
        self.assertEqual(
            self.error_code(role_error),
            'settlement.control.invalid_role',
        )
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

        Role.objects.filter(pk=self.clerk_role.pk).update(
            is_active=True,
            code='control_writer_forbidden',
        )
        with self.assertRaises(ValidationError) as role_code_error:
            self.settle(control_context=context)
        self.assertEqual(
            self.error_code(role_code_error),
            'settlement.control.invalid_role',
        )
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_writer_lock_sequence_uses_common_sorted_plan_before_domain_rows(self):
        context = self.write_context()
        sequence = []
        plans = []
        original_lease = settlement_services.lock_settlement_write_lease
        original_access = settlement_services.lock_settlement_write_access
        original_residents = settlement_services.lock_settlement_residents_after_access
        original_beds = settlement_services._locked_beds
        original_rooms = settlement_services._locked_rooms
        original_occupancies = settlement_services._locked_related_occupancies

        def observed_lease(**kwargs):
            sequence.append('lease')
            return original_lease(**kwargs)

        def observed_access(**kwargs):
            sequence.append('employee_access')
            result = original_access(**kwargs)
            plans.append(tuple(employee.pk for employee in result.employees))
            return result

        def observed_residents(*args, **kwargs):
            sequence.append('residents')
            return original_residents(*args, **kwargs)

        def observed_beds(*bed_ids, **kwargs):
            sequence.append('beds')
            return original_beds(*bed_ids, **kwargs)

        def observed_rooms(*room_ids, **kwargs):
            sequence.append('rooms')
            return original_rooms(*room_ids, **kwargs)

        def observed_occupancies(**kwargs):
            sequence.append('occupancies')
            return original_occupancies(**kwargs)

        with (
            mock.patch.object(
                settlement_services,
                'lock_settlement_write_lease',
                side_effect=observed_lease,
            ),
            mock.patch.object(
                settlement_services,
                'lock_settlement_write_access',
                side_effect=observed_access,
            ),
            mock.patch.object(
                settlement_services,
                'lock_settlement_residents_after_access',
                side_effect=observed_residents,
            ),
            mock.patch.object(
                settlement_services,
                '_locked_beds',
                side_effect=observed_beds,
            ),
            mock.patch.object(
                settlement_services,
                '_locked_rooms',
                side_effect=observed_rooms,
            ),
            mock.patch.object(
                settlement_services,
                '_locked_related_occupancies',
                side_effect=observed_occupancies,
            ),
        ):
            self.settle(control_context=context)

        self.assertEqual(
            sequence,
            ['lease', 'employee_access', 'residents', 'beds', 'rooms', 'occupancies'],
        )
        self.assertEqual(plans[0], tuple(sorted(plans[0])))
        access_source = inspect.getsource(employee_access_locks._lock_accesses)
        self.assertIn("select_for_update(of=('self',))", access_source)
        self.assertIn("order_by('pk')", access_source)
        self.assertNotIn('Role.objects', access_source)
        resident_source = inspect.getsource(
            settlement_residents._lock_residents_with_employees,
        )
        self.assertIn("select_for_update(of=('self',))", resident_source)
        self.assertIn("order_by('pk')", resident_source)
        self.assertNotIn('Employee.objects', resident_source)
        for helper in (
            settlement_services._locked_beds,
            settlement_services._locked_rooms,
            settlement_services._locked_related_occupancies,
        ):
            self.assertIn("order_by('pk')", inspect.getsource(helper))

    def test_every_writer_requires_context_and_orders_control_before_domain_locks(self):
        writer_pairs = (
            (settle_employee_on_bed, settlement_services.settle_resident_on_bed),
            (relocate_employee_to_bed, settlement_services.relocate_resident_to_bed),
            (release_employee_from_bed, settlement_services.release_resident_from_bed),
        )
        for adapter, writer in writer_pairs:
            with self.subTest(writer=adapter.__name__):
                parameters = inspect.signature(adapter).parameters
                self.assertIn('control_context', parameters)
                self.assertNotIn('settled_by', parameters)
                adapter_source = inspect.getsource(adapter)
                if adapter is settle_employee_on_bed:
                    self.assertIn('_resident_id_for_employee(', adapter_source)
                    self.assertNotIn('get_or_create', adapter_source)
                else:
                    self.assertIn('occupancy_id=occupancy_id', adapter_source)
                source = inspect.getsource(writer)
                positions = tuple(
                    source.index(fragment)
                    for fragment in (
                        'lock_settlement_write_lease(',
                        'build_settlement_resident_lock_plan(',
                        'lock_settlement_write_access(',
                        'lock_settlement_residents_after_access(',
                        '_locked_beds(',
                        '_locked_rooms(',
                        'select_for_update(of=(\'self\',))'
                        if writer is settlement_services.release_resident_from_bed
                        else '_locked_related_occupancies(',
                    )
                )
                self.assertEqual(positions, tuple(sorted(positions)))

    def test_stale_access_employee_and_role_mapping_roll_back_without_write(self):
        context = self.write_context()
        original_builder = control_module.build_employee_access_lock_plan

        def reassign_employee(**kwargs):
            plan = original_builder(**kwargs)
            EmployeeAccess.objects.filter(pk=self.clerk_access.pk).update(
                employee=self.subject,
            )
            return plan

        with mock.patch.object(
            control_module,
            'build_employee_access_lock_plan',
            side_effect=reassign_employee,
        ):
            with self.assertRaises(ValidationError) as employee_error:
                self.settle(control_context=context)
        self.assertEqual(
            self.error_code(employee_error),
            'settlement.control.invalid_access',
        )
        self.clerk_access.refresh_from_db()
        self.assertNotEqual(self.clerk_access.employee_id, self.subject.pk)
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

        def reassign_role(**kwargs):
            plan = original_builder(**kwargs)
            EmployeeAccess.objects.filter(pk=self.clerk_access.pk).update(
                role=self.other_role,
            )
            return plan

        with mock.patch.object(
            control_module,
            'build_employee_access_lock_plan',
            side_effect=reassign_role,
        ):
            with self.assertRaises(ValidationError) as role_error:
                self.settle(control_context=context)
        self.assertEqual(
            self.error_code(role_error),
            'settlement.control.invalid_access',
        )
        self.clerk_access.refresh_from_db()
        self.assertEqual(self.clerk_access.role_id, self.clerk_role.pk)
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_stale_domain_plan_is_rejected_and_rolled_back(self):
        context = self.write_context()
        original_access_lock = settlement_services.lock_settlement_write_access

        def move_bed_after_preread(**kwargs):
            result = original_access_lock(**kwargs)
            PhysicalBed.objects.filter(pk=self.first_bed.pk).update(
                room=self.second_room,
            )
            return result

        with mock.patch.object(
            settlement_services,
            'lock_settlement_write_access',
            side_effect=move_bed_after_preread,
        ):
            with self.assertRaises(ValidationError) as bed_error:
                self.settle(control_context=context)
        self.assertEqual(
            self.error_code(bed_error),
            'settlement_occupancy_changed',
        )
        self.first_bed.refresh_from_db()
        self.assertEqual(self.first_bed.room_id, self.first_room.pk)
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

        def add_occupancy_after_preread(**kwargs):
            result = original_access_lock(**kwargs)
            self.create_existing_occupancy()
            return result

        with mock.patch.object(
            settlement_services,
            'lock_settlement_write_access',
            side_effect=add_occupancy_after_preread,
        ):
            with self.assertRaises(ValidationError) as occupancy_error:
                self.settle(control_context=context)
        self.assertEqual(
            self.error_code(occupancy_error),
            'settlement_occupancy_changed',
        )
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_exception_after_all_locks_rolls_back_without_partial_occupancy(self):
        context = self.write_context()
        forced_error = RuntimeError('controlled writer failure after locks')

        with mock.patch.object(
            settlement_services,
            '_create_occupancy',
            side_effect=forced_error,
        ):
            with self.assertRaises(RuntimeError) as error:
                self.settle(control_context=context)

        self.assertIs(error.exception, forced_error)
        self.assertFalse(EmployeeBedOccupancy.objects.exists())


class SettlementControlLockOrderTests(
    SettlementControlFixtureMixin,
    TestCase,
):
    @classmethod
    def setUpTestData(cls):
        cls.create_control_fixtures()

    def setUp(self):
        SettlementControlLease.objects.filter(scope='settlement').update(
            owner_access=None,
            owner_session_hash='',
            lease_token=None,
            fencing_revision=0,
            acquired_at=None,
            heartbeat_at=None,
            expires_at=None,
        )
        self.now = timezone.now()

    def assert_postgresql_control_lock_order(self, action):
        if connection.vendor != 'postgresql':
            self.skipTest('Exact control lock SQL is PostgreSQL-only.')

        with CaptureQueriesContext(connection) as captured:
            action()

        sql_statements = [query['sql'].upper() for query in captured]
        lease_positions = [
            index
            for index, sql in enumerate(sql_statements)
            if 'FROM "SETTLEMENT_SETTLEMENTCONTROLLEASE"' in sql
            and 'FOR UPDATE' in sql
        ]
        preread_positions = [
            index
            for index, sql in enumerate(sql_statements)
            if 'FROM "USERS_EMPLOYEEACCESS"' in sql
            and 'FOR UPDATE' not in sql
        ]
        employee_lock_positions = [
            index
            for index, sql in enumerate(sql_statements)
            if 'FROM "USERS_EMPLOYEE"' in sql
            and 'FOR UPDATE' in sql
        ]
        access_lock_positions = [
            index
            for index, sql in enumerate(sql_statements)
            if 'FROM "USERS_EMPLOYEEACCESS"' in sql
            and 'FOR UPDATE' in sql
        ]
        self.assertEqual(len(lease_positions), 1, sql_statements)
        self.assertEqual(len(preread_positions), 1, sql_statements)
        self.assertEqual(len(employee_lock_positions), 1, sql_statements)
        self.assertEqual(len(access_lock_positions), 1, sql_statements)
        positions = (
            lease_positions[0],
            preread_positions[0],
            employee_lock_positions[0],
            access_lock_positions[0],
        )
        self.assertEqual(positions, tuple(sorted(positions)), sql_statements)
        self.assertIn(
            'FOR UPDATE OF "USERS_EMPLOYEE"',
            sql_statements[employee_lock_positions[0]],
        )
        access_sql = sql_statements[access_lock_positions[0]]
        self.assertIn('FOR UPDATE OF "USERS_EMPLOYEEACCESS"', access_sql)
        access_lock_clause = access_sql.split('FOR UPDATE OF', 1)[1]
        self.assertNotIn('"USERS_EMPLOYEE"', access_lock_clause)
        self.assertNotIn('"USERS_ROLE"', access_lock_clause)
        self.assertFalse(any(
            position > access_lock_positions[0]
            for position in employee_lock_positions
        ))

    def test_acquire_uses_lease_employee_access_order(self):
        self.assert_postgresql_control_lock_order(lambda: acquire_control_lease(
            owner_access_id=self.clerk_access.pk,
            raw_session_key='lock-order-clerk-session',
            source='lock-order-acquire',
            now=self.now,
            ttl=timedelta(minutes=5),
        ))

    def test_takeover_uses_lease_employee_access_order(self):
        acquire_control_lease(
            owner_access_id=self.clerk_access.pk,
            raw_session_key='lock-order-owner-session',
            source='lock-order-setup',
            now=self.now,
            ttl=timedelta(minutes=5),
        )

        self.assert_postgresql_control_lock_order(lambda: takeover_control_lease(
            admin_access_id=self.admin_access.pk,
            raw_session_key='lock-order-admin-session',
            reason='Canonical lock-order takeover',
            source='lock-order-takeover',
            now=self.now + timedelta(seconds=1),
            ttl=timedelta(minutes=5),
        ))


class SettlementControlConcurrencyTests(
    SettlementControlFixtureMixin,
    TransactionTestCase,
):
    def setUp(self):
        self.create_control_fixtures()
        self.second_admin_access = self.create_access(
            'SECOND-ADMIN',
            self.admin_role,
        )
        self.now = timezone.now()

    @staticmethod
    def acquire_in_thread(*, barrier, access_id, session_key, now):
        close_old_connections()
        try:
            with connections['default'].cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '15s'")
            barrier.wait(timeout=10)
            try:
                grant = acquire_control_lease(
                    owner_access_id=access_id,
                    raw_session_key=session_key,
                    source='test-concurrency',
                    session_metadata={'request_id': session_key},
                    now=now,
                    ttl=timedelta(minutes=5),
                )
            except ValidationError as error:
                codes = SettlementControlFixtureMixin.validation_codes(error)
                return ('validation', tuple(sorted(codes)))
            except Exception as error:  # Returned for an exact fail-fast assertion.
                return ('unexpected', type(error).__name__, str(error))
            return (
                'acquired',
                access_id,
                str(grant.lease_token),
                grant.fencing_revision,
            )
        finally:
            connections['default'].close()

    @staticmethod
    def control_action_in_thread(*, barrier, action):
        close_old_connections()
        try:
            with connections['default'].cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '15s'")
            barrier.wait(timeout=10)
            try:
                result = action()
            except ValidationError as error:
                codes = SettlementControlFixtureMixin.validation_codes(error)
                return ('validation', tuple(sorted(codes)))
            except Exception as error:  # Returned for an exact fail-fast assertion.
                return ('unexpected', type(error).__name__, str(error))
            if result is None:
                return ('success', 'none')
            if isinstance(result, ControlLeaseGrant):
                return (
                    'success',
                    'grant',
                    str(result.lease_token),
                    result.fencing_revision,
                )
            return (
                'success',
                'transition',
                result.event_type,
                result.fencing_revision,
            )
        finally:
            connections['default'].close()

    def run_control_actions(self, *actions):
        barrier = threading.Barrier(len(actions))
        with ThreadPoolExecutor(max_workers=len(actions)) as executor:
            futures = [
                executor.submit(
                    self.control_action_in_thread,
                    barrier=barrier,
                    action=action,
                )
                for action in actions
            ]
            return [future.result(timeout=20) for future in futures]

    def assert_no_unexpected_concurrency_result(self, results):
        self.assertFalse(
            any(result[0] == 'unexpected' for result in results),
            results,
        )
        self.assertTrue(all(result[0] in {'success', 'validation'} for result in results))

    def run_two_acquires(self):
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(
                    self.acquire_in_thread,
                    barrier=barrier,
                    access_id=self.clerk_access.pk,
                    session_key='concurrent-session-one',
                    now=self.now,
                ),
                executor.submit(
                    self.acquire_in_thread,
                    barrier=barrier,
                    access_id=self.second_clerk_access.pk,
                    session_key='concurrent-session-two',
                    now=self.now,
                ),
            )
            return [future.result(timeout=20) for future in futures]

    def assert_single_concurrency_winner(self, results):
        self.assertEqual(
            sorted(result[0] for result in results),
            ['acquired', 'validation'],
        )
        loser = next(result for result in results if result[0] == 'validation')
        self.assertEqual(loser, ('validation', ('settlement.control.busy',)))
        self.assertFalse(any(result[0] == 'unexpected' for result in results))

        self.assertEqual(
            SettlementControlLease.objects.filter(scope='settlement').count(),
            1,
        )
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertIn(
            lease.owner_access_id,
            {self.clerk_access.pk, self.second_clerk_access.pk},
        )
        self.assertEqual(lease.fencing_revision, 1)
        self.assertIsNotNone(lease.lease_token)
        events = list(
            SettlementControlEvent.objects
            .filter(scope='settlement')
            .values('event_type', 'new_owner_access_id')
        )
        self.assertEqual(events, [{
            'event_type': SettlementControlEvent.EventType.ACQUIRED,
            'new_owner_access_id': lease.owner_access_id,
        }])

    def test_two_concurrent_acquires_recover_missing_singleton_safely(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Concurrent singleton creation is PostgreSQL-only.')
        SettlementControlLease.objects.filter(scope='settlement').delete()

        results = self.run_two_acquires()

        self.assert_single_concurrency_winner(results)

    def test_two_concurrent_acquires_serialize_on_free_singleton(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Concurrent row locking is PostgreSQL-only.')
        ensure_control_lease()

        results = self.run_two_acquires()

        self.assert_single_concurrency_winner(results)

    def test_two_admin_takeovers_serialize_with_strict_revisions(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Concurrent takeover is PostgreSQL-only.')
        acquire_control_lease(
            owner_access_id=self.clerk_access.pk,
            raw_session_key='concurrent-clerk-session',
            source='concurrent-setup',
            now=self.now,
            ttl=timedelta(minutes=5),
        )
        admin_cases = (
            (self.admin_access, 'concurrent-admin-one'),
            (self.second_admin_access, 'concurrent-admin-two'),
        )

        results = self.run_control_actions(*(
            lambda access=access, session=session: takeover_control_lease(
                admin_access_id=access.pk,
                raw_session_key=session,
                reason=f'Approved takeover for {access.pk}',
                source='concurrent-takeover',
                now=self.now + timedelta(seconds=1),
                ttl=timedelta(minutes=5),
            )
            for access, session in admin_cases
        ))

        self.assert_no_unexpected_concurrency_result(results)
        self.assertTrue(all(result[:2] == ('success', 'grant') for result in results))
        self.assertEqual(sorted(result[3] for result in results), [2, 3])
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.fencing_revision, 3)
        self.assertIn(
            lease.owner_access_id,
            {self.admin_access.pk, self.second_admin_access.pk},
        )
        events = self.event_state()
        self.assertEqual(
            [event['event_type'] for event in events],
            [
                SettlementControlEvent.EventType.ACQUIRED,
                SettlementControlEvent.EventType.TAKEN_OVER,
                SettlementControlEvent.EventType.TAKEN_OVER,
            ],
        )
        self.assertEqual(
            [
                (
                    event['previous_fencing_revision'],
                    event['new_fencing_revision'],
                )
                for event in events
            ],
            [(0, 1), (1, 2), (2, 3)],
        )

        first_index = next(index for index, result in enumerate(results) if result[3] == 2)
        final_index = next(index for index, result in enumerate(results) if result[3] == 3)
        final_access, final_session = admin_cases[final_index]
        self.assert_control_error(
            'settlement.control.invalid_token',
            heartbeat_control_lease,
            owner_access_id=final_access.pk,
            raw_session_key=final_session,
            lease_token=results[first_index][2],
            fencing_revision=3,
            now=self.now + timedelta(seconds=2),
        )

    def test_takeover_and_old_owner_heartbeat_serialize(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Concurrent takeover is PostgreSQL-only.')
        grant = acquire_control_lease(
            owner_access_id=self.clerk_access.pk,
            raw_session_key='heartbeat-clerk-session',
            source='concurrent-setup',
            now=self.now,
            ttl=timedelta(minutes=5),
        )

        results = self.run_control_actions(
            lambda: takeover_control_lease(
                admin_access_id=self.admin_access.pk,
                raw_session_key='heartbeat-admin-session',
                reason='Heartbeat race takeover',
                source='concurrent-takeover',
                now=self.now + timedelta(seconds=1),
            ),
            lambda: heartbeat_control_lease(
                owner_access_id=self.clerk_access.pk,
                raw_session_key='heartbeat-clerk-session',
                lease_token=grant.lease_token,
                fencing_revision=grant.fencing_revision,
                now=self.now + timedelta(seconds=1),
            ),
        )

        self.assert_no_unexpected_concurrency_result(results)
        self.assertEqual(results[0][:2], ('success', 'grant'))
        self.assertIn(
            results[1],
            {
                ('success', 'grant', str(grant.lease_token), grant.fencing_revision),
                ('validation', ('settlement.control.busy',)),
            },
        )
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.owner_access_id, self.admin_access.pk)
        self.assertEqual(lease.fencing_revision, 2)
        self.assertEqual(
            [event['event_type'] for event in self.event_state()],
            [
                SettlementControlEvent.EventType.ACQUIRED,
                SettlementControlEvent.EventType.TAKEN_OVER,
            ],
        )

    @staticmethod
    def concurrency_exception_signature(error):
        current = error
        seen = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            code = getattr(current, 'sqlstate', None) or getattr(current, 'pgcode', None)
            if code:
                return (type(error).__name__, code)
            current = getattr(current, '__cause__', None) or getattr(
                current,
                '__context__',
                None,
            )
        return (type(error).__name__, None)

    def run_service_against_canonical_employee_access_lock(
        self,
        *,
        employee_id,
        access_id,
        action,
    ):
        employee_locked = threading.Event()
        service_employee_lock_attempted = threading.Event()
        original_lock_employees = employee_access_locks._lock_employees

        def observed_lock_employees(plan):
            service_employee_lock_attempted.set()
            return original_lock_employees(plan)

        def canonical_holder():
            close_old_connections()
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '15s'")
                with transaction.atomic():
                    Employee.objects.select_for_update(of=('self',)).get(
                        pk=employee_id,
                    )
                    employee_locked.set()
                    if not service_employee_lock_attempted.wait(timeout=10):
                        raise TimeoutError('Service did not attempt the Employee lock.')
                    EmployeeAccess.objects.select_for_update(of=('self',)).get(
                        pk=access_id,
                    )
                return ('success', 'canonical')
            except Exception as error:
                return ('unexpected', *self.concurrency_exception_signature(error))
            finally:
                connections['default'].close()

        def service_worker():
            close_old_connections()
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '15s'")
                result = action()
                return (
                    'success',
                    'service',
                    result.fencing_revision,
                    str(result.lease_token),
                )
            except ValidationError as error:
                return ('validation', tuple(sorted(self.validation_codes(error))))
            except Exception as error:
                return ('unexpected', *self.concurrency_exception_signature(error))
            finally:
                connections['default'].close()

        with mock.patch.object(
            employee_access_locks,
            '_lock_employees',
            side_effect=observed_lock_employees,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                holder = executor.submit(canonical_holder)
                self.assertTrue(employee_locked.wait(timeout=10))
                service = executor.submit(service_worker)
                results = [
                    holder.result(timeout=20),
                    service.result(timeout=20),
                ]

        self.assertFalse(any(result[0] == 'unexpected' for result in results), results)
        self.assertEqual(results[0], ('success', 'canonical'))
        self.assertEqual(results[1][:2], ('success', 'service'))
        return results[1]

    def test_acquire_is_compatible_with_canonical_employee_access_transaction(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL row-lock behavior is required.')
        ensure_control_lease()

        service_result = self.run_service_against_canonical_employee_access_lock(
            employee_id=self.clerk_access.employee_id,
            access_id=self.clerk_access.pk,
            action=lambda: acquire_control_lease(
                owner_access_id=self.clerk_access.pk,
                raw_session_key='canonical-acquire-session',
                source='canonical-acquire',
                now=self.now,
                ttl=timedelta(minutes=5),
            ),
        )

        self.assertEqual(service_result[2], 1)
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.owner_access_id, self.clerk_access.pk)
        self.assertEqual(lease.fencing_revision, 1)
        self.assertEqual(
            [event['event_type'] for event in self.event_state()],
            [SettlementControlEvent.EventType.ACQUIRED],
        )

    def test_takeover_is_compatible_with_canonical_employee_access_transaction(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL row-lock behavior is required.')
        acquire_control_lease(
            owner_access_id=self.clerk_access.pk,
            raw_session_key='canonical-owner-session',
            source='canonical-setup',
            now=self.now,
            ttl=timedelta(minutes=5),
        )

        service_result = self.run_service_against_canonical_employee_access_lock(
            employee_id=self.admin_access.employee_id,
            access_id=self.admin_access.pk,
            action=lambda: takeover_control_lease(
                admin_access_id=self.admin_access.pk,
                raw_session_key='canonical-admin-session',
                reason='Canonical Employee then Access transaction',
                source='canonical-takeover',
                now=self.now + timedelta(seconds=1),
                ttl=timedelta(minutes=5),
            ),
        )

        self.assertEqual(service_result[2], 2)
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.owner_access_id, self.admin_access.pk)
        self.assertEqual(lease.fencing_revision, 2)
        self.assertEqual(
            [event['event_type'] for event in self.event_state()],
            [
                SettlementControlEvent.EventType.ACQUIRED,
                SettlementControlEvent.EventType.TAKEN_OVER,
            ],
        )

    def test_access_reassignment_after_preread_fails_without_partial_control_write(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL transaction interleaving is required.')
        ensure_control_lease()
        replacement_employee = Employee.objects.create(
            full_name='Control reassignment replacement',
            personnel_number='CONTROL-REASSIGN',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        before_lease = self.lease_state()
        before_events = self.event_state()
        preread_complete = threading.Event()
        allow_locking = threading.Event()
        original_builder = control_module.build_employee_access_lock_plan

        def paused_builder(*args, **kwargs):
            plan = original_builder(*args, **kwargs)
            preread_complete.set()
            if not allow_locking.wait(timeout=10):
                raise TimeoutError('Access reassignment was not released.')
            return plan

        def acquiring_worker():
            close_old_connections()
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '15s'")
                try:
                    acquire_control_lease(
                        owner_access_id=self.clerk_access.pk,
                        raw_session_key='reassigned-access-session',
                        source='reassigned-access',
                        now=self.now,
                        ttl=timedelta(minutes=5),
                    )
                except ValidationError as error:
                    return ('validation', tuple(sorted(self.validation_codes(error))))
                return ('unexpected', 'acquire_succeeded', None)
            except Exception as error:
                return ('unexpected', *self.concurrency_exception_signature(error))
            finally:
                connections['default'].close()

        with mock.patch.object(
            control_module,
            'build_employee_access_lock_plan',
            side_effect=paused_builder,
        ):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(acquiring_worker)
                self.assertTrue(preread_complete.wait(timeout=10))
                updated = EmployeeAccess.objects.filter(
                    pk=self.clerk_access.pk,
                ).update(employee=replacement_employee)
                self.assertEqual(updated, 1)
                allow_locking.set()
                result = future.result(timeout=20)

        self.assertEqual(
            result,
            ('validation', ('settlement.control.invalid_access',)),
        )
        self.assertEqual(self.lease_state(), before_lease)
        self.assertEqual(self.event_state(), before_events)
        self.assertEqual(
            EmployeeAccess.objects.get(pk=self.clerk_access.pk).employee_id,
            replacement_employee.pk,
        )

    def test_takeover_and_old_owner_release_serialize(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Concurrent takeover is PostgreSQL-only.')
        grant = acquire_control_lease(
            owner_access_id=self.clerk_access.pk,
            raw_session_key='release-clerk-session',
            source='concurrent-setup',
            now=self.now,
            ttl=timedelta(minutes=5),
        )

        results = self.run_control_actions(
            lambda: takeover_control_lease(
                admin_access_id=self.admin_access.pk,
                raw_session_key='release-admin-session',
                reason='Release race takeover',
                source='concurrent-takeover',
                now=self.now + timedelta(seconds=1),
            ),
            lambda: release_control_lease(
                owner_access_id=self.clerk_access.pk,
                raw_session_key='release-clerk-session',
                lease_token=grant.lease_token,
                fencing_revision=grant.fencing_revision,
                source='concurrent-release',
                now=self.now + timedelta(seconds=1),
            ),
        )

        self.assert_no_unexpected_concurrency_result(results)
        self.assertEqual(results[0][:2], ('success', 'grant'))
        self.assertIn(
            results[1],
            {
                (
                    'success',
                    'transition',
                    SettlementControlEvent.EventType.RELEASED,
                    2,
                ),
                ('validation', ('settlement.control.busy',)),
            },
        )
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.owner_access_id, self.admin_access.pk)
        events = self.event_state()
        event_types = [event['event_type'] for event in events]
        self.assertIn(
            event_types,
            [
                [
                    SettlementControlEvent.EventType.ACQUIRED,
                    SettlementControlEvent.EventType.TAKEN_OVER,
                ],
                [
                    SettlementControlEvent.EventType.ACQUIRED,
                    SettlementControlEvent.EventType.RELEASED,
                    SettlementControlEvent.EventType.ACQUIRED,
                ],
            ],
        )
        self.assertEqual(lease.fencing_revision, events[-1]['new_fencing_revision'])

    def test_takeover_and_expiry_create_one_expired_and_one_acquired(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Concurrent takeover is PostgreSQL-only.')
        acquire_control_lease(
            owner_access_id=self.clerk_access.pk,
            raw_session_key='expiry-clerk-session',
            source='concurrent-setup',
            now=self.now,
            ttl=timedelta(seconds=1),
        )
        operation_time = self.now + timedelta(seconds=2)

        results = self.run_control_actions(
            lambda: takeover_control_lease(
                admin_access_id=self.admin_access.pk,
                raw_session_key='expiry-admin-session',
                reason='Expiry race takeover',
                source='concurrent-takeover',
                now=operation_time,
            ),
            lambda: expire_control_lease(
                source='concurrent-expiry',
                now=operation_time,
            ),
        )

        self.assert_no_unexpected_concurrency_result(results)
        self.assertEqual(results[0][:2], ('success', 'grant'))
        self.assertEqual(
            [event['event_type'] for event in self.event_state()],
            [
                SettlementControlEvent.EventType.ACQUIRED,
                SettlementControlEvent.EventType.EXPIRED,
                SettlementControlEvent.EventType.ACQUIRED,
            ],
        )
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.owner_access_id, self.admin_access.pk)
        self.assertEqual(lease.fencing_revision, 3)

    def test_takeover_waits_for_existing_lease_row_lock(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL lock wait introspection is required.')
        acquire_control_lease(
            owner_access_id=self.clerk_access.pk,
            raw_session_key='locked-clerk-session',
            source='concurrent-setup',
            now=self.now,
            ttl=timedelta(minutes=5),
        )
        lease_locked = threading.Event()
        release_lock = threading.Event()
        waiter_started = threading.Event()
        waiter_pid = []

        def hold_lease_lock():
            close_old_connections()
            try:
                with transaction.atomic():
                    SettlementControlLease.objects.select_for_update().get(
                        scope='settlement',
                    )
                    lease_locked.set()
                    if not release_lock.wait(timeout=10):
                        raise TimeoutError('Lease lock release signal was not received.')
            finally:
                connections['default'].close()

        def waiting_takeover():
            close_old_connections()
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '15s'")
                    cursor.execute('SELECT pg_backend_pid()')
                    waiter_pid.append(cursor.fetchone()[0])
                waiter_started.set()
                return takeover_control_lease(
                    admin_access_id=self.admin_access.pk,
                    raw_session_key='locked-admin-session',
                    reason='Wait for in-flight lease transaction',
                    source='concurrent-takeover',
                    now=self.now + timedelta(seconds=1),
                )
            finally:
                connections['default'].close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            lock_future = executor.submit(hold_lease_lock)
            self.assertTrue(lease_locked.wait(timeout=10))
            takeover_future = executor.submit(waiting_takeover)
            self.assertTrue(waiter_started.wait(timeout=10))

            deadline = time.monotonic() + 5
            observed_lock_wait = False
            while time.monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute(
                        'SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s',
                        [waiter_pid[0]],
                    )
                    row = cursor.fetchone()
                if row and row[0] == 'Lock':
                    observed_lock_wait = True
                    break
                time.sleep(0.05)
            self.assertTrue(observed_lock_wait)
            self.assertFalse(takeover_future.done())
            release_lock.set()
            lock_future.result(timeout=10)
            grant = takeover_future.result(timeout=20)

        self.assertIsInstance(grant, ControlLeaseGrant)
        self.assertEqual(grant.fencing_revision, 2)
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.owner_access_id, self.admin_access.pk)
        self.assertEqual(
            [event['event_type'] for event in self.event_state()],
            [
                SettlementControlEvent.EventType.ACQUIRED,
                SettlementControlEvent.EventType.TAKEN_OVER,
            ],
        )
