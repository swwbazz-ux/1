import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from users.models import Employee, EmployeeAccess, Role

from .control import (
    ControlLeaseGrant,
    acquire_control_lease,
    ensure_control_lease,
    expire_control_lease,
    heartbeat_control_lease,
    release_control_lease,
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


class SettlementControlConcurrencyTests(
    SettlementControlFixtureMixin,
    TransactionTestCase,
):
    def setUp(self):
        self.create_control_fixtures()
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
