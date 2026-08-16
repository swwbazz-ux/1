import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from users import employee_access_locks
from users.models import Employee, EmployeeAccess, Role

from . import control as control_module
from .control import (
    ControlLeaseGrant,
    acquire_control_lease,
    ensure_control_lease,
    expire_control_lease,
    heartbeat_control_lease,
    release_control_lease,
    takeover_control_lease,
)
from .models import SettlementControlEvent, SettlementControlLease


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
