import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from asgiref.sync import sync_to_async
from django.core.management import call_command
from django.db import IntegrityError, connection, connections
from django.http import JsonResponse
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import reverse
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    EquipmentAssignment,
    HaulAssignment,
    HaulAssignmentAction,
)
from references.models import DumpPoint, Equipment, EquipmentType, RockType
from shifts.models import EmployeeShift, ShiftType
from trips.models import Trip, TripStatus
from users.models import Employee, EmployeeAccess, Role

from .models import OperationalStateEvent, OperationalStateVersion, bump_operational_state
from .snapshot import operational_snapshot_view
from .sse import (
    StreamRejected,
    applied_cursor_from_request,
    build_cursor_context,
    encode_sse,
    operational_journal_status,
    operational_event_stream,
    refresh_stream_database_snapshot,
)


class _StreamConnectionCleanupMixin:
    """Закрывает соединения, которые SSE-поток открыл вне потока теста.

    ORM внутри стрима выполняется через
    ``sync_to_async(..., thread_sensitive=True)``, поэтому asgiref уводит его
    в отдельный долгоживущий поток (``SyncToAsync.single_thread_executor``) со
    своим соединением к PostgreSQL. Django при завершении теста закрывает
    соединения только текущего потока, поэтому без явного закрытия
    ``DROP DATABASE`` тестовой базы получает ``ObjectInUse`` — тот же класс
    проблемы, который T-021 закрыл для revocation-теста.
    """

    def tearDown(self):
        self._close_stream_connections()
        super().tearDown()

    @classmethod
    def tearDownClass(cls):
        cls._close_stream_connections()
        super().tearDownClass()

    @staticmethod
    def _close_stream_connections():
        connections.close_all()
        asyncio.run(
            sync_to_async(connections.close_all, thread_sensitive=True)(),
        )


class SseProtocolUnitTests(_StreamConnectionCleanupMixin, SimpleTestCase):
    databases = {'default'}

    def test_explicit_applied_then_after_precede_last_event_id(self):
        factory = RequestFactory()
        request = factory.get(
            '/realtime/stream/?applied=7&after=8',
            HTTP_LAST_EVENT_ID='9',
        )
        self.assertEqual(applied_cursor_from_request(request), (7, 'applied'))

        request = factory.get(
            '/realtime/stream/?after=8',
            HTTP_LAST_EVENT_ID='9',
        )
        self.assertEqual(applied_cursor_from_request(request), (8, 'after'))

        request = factory.get('/realtime/stream/', HTTP_LAST_EVENT_ID='9')
        self.assertEqual(applied_cursor_from_request(request), (9, 'last_event_id'))

    def test_heartbeat_is_named_data_event_visible_to_event_source(self):
        frame = encode_sse('heartbeat', {'version': 4, 'alive': True}).decode()
        self.assertIn('event: heartbeat\n', frame)
        self.assertIn('data: {"version":4,"alive":true}\n\n', frame)
        self.assertNotEqual(frame[0], ':')

    def test_snapshot_wrapper_opens_atomic_snapshot_only_when_requested(self):
        seen = []

        def view(_request):
            seen.append(connection.in_atomic_block)
            return JsonResponse({'ok': True})

        wrapped = operational_snapshot_view(view)
        request = RequestFactory().get(
            '/driver/',
            {'_operational_fragment': 'driver', '_operational_snapshot': '1'},
        )
        response = wrapped(request)
        self.assertEqual(response['X-Operational-Snapshot'], 'repeatable-read-v1')
        self.assertEqual(seen, [True])

    def test_subscription_is_confirmed_before_initial_database_replay(self):
        order = []

        class Subscription:
            async def connect(self):
                order.append('subscribed')
                return True

            async def wait(self, _timeout):
                return None

            async def close(self):
                order.append('closed')

        context = {
            'access': SimpleNamespace(id=1, employee_id=2),
            'access_id': 1,
            'employee_id': 2,
            'role_app_code': 'driver',
            'role_active': True,
            'session_revision': 'r1',
            'active_shift_id': '',
            'has_active_shift': False,
            'background_connection_required': False,
            'own_equipment_ids': [],
            'worker_equipment_ids': [],
            'cursor_scope': 'scope-1',
            'snapshot_url': '/driver/?_operational_snapshot=1',
        }
        journal = {
            'epoch': 'epoch-1',
            'min_version': None,
            'max_version': 0,
            'event_max_version': None,
            'reason': '',
            'updated_at': '',
        }

        def read_page(_context, cursor, _batch_size):
            order.append('db-replay')
            self.assertEqual(order[0], 'subscribed')
            return 'caught_up', journal, cursor

        async def first_frame():
            with (
                patch('core.sse._resolve_stream_context', return_value=context),
                patch('core.sse.operational_journal_status', return_value=journal),
                patch('core.sse._read_page', side_effect=read_page),
                override_settings(
                    SSE_STREAM_MAX_SECONDS=2,
                    SSE_REAUTH_SECONDS=10,
                    SSE_HEARTBEAT_SECONDS=10,
                    SSE_SAFETY_SCAN_SECONDS=10,
                    SSE_BATCH_SIZE=2,
                ),
            ):
                stream = operational_event_stream(
                    object(),
                    initial_context=context,
                    cursor=0,
                    subscriber_factory=Subscription,
                )
                try:
                    return await stream.__anext__()
                finally:
                    await stream.aclose()

        frame = asyncio.run(first_frame()).decode('utf-8')
        self.assertIn('event: heartbeat', frame)
        self.assertEqual(order[:2], ['subscribed', 'db-replay'])
        self.assertEqual(order[-1], 'closed')

    def _stream_context(self, *, scope='scope-1', equipment_ids=None):
        return {
            'access': SimpleNamespace(id=1, employee_id=2),
            'access_id': 1,
            'employee_id': 2,
            'role_app_code': 'driver',
            'role_active': True,
            'session_revision': 'r1',
            'active_shift_id': '',
            'has_active_shift': False,
            'background_connection_required': False,
            'own_equipment_ids': [],
            'worker_equipment_ids': equipment_ids or [],
            'cursor_scope': scope,
            'snapshot_url': '/driver/?_operational_snapshot=1',
        }

    def _journal(self, *, epoch='epoch-1', version=0):
        return {
            'epoch': epoch,
            'min_version': 1 if version else None,
            'max_version': version,
            'event_max_version': version if version else None,
            'reason': '',
            'updated_at': '',
        }

    def _collect_replay_boundary(self, *, mutate, resolve=None, journal_status=None, clock=None):
        original = self._stream_context(scope='scope-1', equipment_ids=[10])
        state = {'changed': False, 'read_calls': 0}
        journal_status = journal_status or (
            lambda: self._journal(epoch='epoch-1', version=2)
        )
        resolve = resolve or (lambda _request: original)
        clock = clock or (lambda: 0.0)

        class Subscription:
            async def connect(self):
                return True

            async def wait(self, _timeout):
                return None

            async def close(self):
                return None

        def read_page(_context, cursor, _batch_size):
            state['read_calls'] += 1
            version = cursor + 1
            return (
                'page',
                {
                    'version': version,
                    'events': [{'version': version, 'type': 'reference_changed'}],
                    'relevant': True,
                },
                version,
            )

        async def collect():
            with (
                patch('core.sse._resolve_stream_context', side_effect=resolve),
                patch('core.sse.operational_journal_status', side_effect=journal_status),
                patch('core.sse._read_page', side_effect=read_page),
                patch('core.sse.refresh_stream_database_snapshot', return_value=None),
                patch('core.sse.time.monotonic', side_effect=clock),
                override_settings(
                    SSE_STREAM_MAX_SECONDS=60,
                    SSE_REAUTH_SECONDS=10,
                    SSE_HEARTBEAT_SECONDS=10,
                    SSE_SAFETY_SCAN_SECONDS=10,
                    SSE_BATCH_SIZE=1,
                ),
            ):
                stream = operational_event_stream(
                    object(), initial_context=original, cursor=0,
                    subscriber_factory=Subscription,
                )
                try:
                    first = await stream.__anext__()
                    mutate(state)
                    try:
                        second = await stream.__anext__()
                    except StopAsyncIteration:
                        second = None
                    return first, second, state['read_calls']
                finally:
                    await stream.aclose()

        return asyncio.run(collect())

    def test_multi_page_replay_rechecks_revoked_access_before_second_page(self):
        state_ref = {'changed': False}

        def resolve(_request):
            if state_ref['changed']:
                raise StreamRejected('authentication_required', status=401)
            return self._stream_context(scope='scope-1', equipment_ids=[10])

        def mutate(state):
            state['changed'] = True
            state_ref['changed'] = True

        first, second, read_calls = self._collect_replay_boundary(
            mutate=mutate,
            resolve=resolve,
        )
        self.assertIn(b'id: 1', first)
        self.assertIn(b'event: reset', second)
        self.assertIn(b'authentication_required', second)
        self.assertEqual(read_calls, 1)

    def test_multi_page_replay_rechecks_context_before_second_page(self):
        original = self._stream_context(scope='scope-1', equipment_ids=[10])
        changed = self._stream_context(scope='scope-2', equipment_ids=[20])
        state_ref = {'changed': False}

        first, second, read_calls = self._collect_replay_boundary(
            mutate=lambda state: state_ref.update(changed=True),
            resolve=lambda _request: changed if state_ref['changed'] else original,
        )
        self.assertIn(b'id: 1', first)
        self.assertIn(b'role_context_changed', second)
        self.assertEqual(read_calls, 1)

    def test_multi_page_replay_rechecks_epoch_before_second_page(self):
        state_ref = {'changed': False}

        first, second, read_calls = self._collect_replay_boundary(
            mutate=lambda state: state_ref.update(changed=True),
            journal_status=lambda: self._journal(
                epoch='epoch-2' if state_ref['changed'] else 'epoch-1',
                version=2,
            ),
        )
        self.assertIn(b'id: 1', first)
        self.assertIn(b'journal_epoch_changed', second)
        self.assertEqual(read_calls, 1)

    def test_multi_page_replay_honours_stream_lifetime_before_second_page(self):
        state_ref = {'expired': False}

        first, second, read_calls = self._collect_replay_boundary(
            mutate=lambda state: state_ref.update(expired=True),
            clock=lambda: 61.0 if state_ref['expired'] else 0.0,
        )
        self.assertIn(b'id: 1', first)
        self.assertIsNone(second)
        self.assertEqual(read_calls, 1)

    def test_related_haul_equipment_does_not_change_cursor_identity_scope(self):
        access = SimpleNamespace(id=1, employee_id=2)
        before = build_cursor_context(
            access=access,
            role_app_code='driver',
            worker_equipment_ids=[10],
            cursor_equipment_ids=[10],
            active_shift_id='3',
            session_revision='r1',
        )
        assigned = build_cursor_context(
            access=access,
            role_app_code='driver',
            worker_equipment_ids=[10, 20],
            cursor_equipment_ids=[10],
            active_shift_id='3',
            session_revision='r1',
        )

        self.assertEqual(before['cursor_scope'], assigned['cursor_scope'])
        self.assertEqual(assigned['worker_equipment_ids'], [10, 20])
        self.assertEqual(assigned['cursor_equipment_ids'], [10])

    def _collect_dynamic_assignment_frame(self, *, before_ids, after_ids, action):
        access = SimpleNamespace(id=1, employee_id=2)
        scope = build_cursor_context(
            access=access,
            role_app_code='driver',
            worker_equipment_ids=before_ids,
            cursor_equipment_ids=[10],
            active_shift_id='',
            session_revision='r1',
        )['cursor_scope']
        original = self._stream_context(scope=scope, equipment_ids=before_ids)
        original['own_equipment_ids'] = [10]
        changed = self._stream_context(scope=scope, equipment_ids=after_ids)
        changed['own_equipment_ids'] = [10]
        state = {'changed': False}

        class Subscription:
            async def connect(self):
                return True

            async def wait(self, _timeout):
                state['changed'] = True
                return None

            async def close(self):
                return None

        journal0 = self._journal()
        journal1 = self._journal(version=1)

        def resolve(_request):
            return changed if state['changed'] else original

        def journal_status():
            return journal1 if state['changed'] else journal0

        def read_page(context, cursor, _batch_size):
            if state['changed'] and cursor == 0:
                self.assertEqual(context['worker_equipment_ids'], after_ids)
                return (
                    'page',
                    {
                        'version': 1,
                        'events': [{
                            'version': 1,
                            'type': 'assignment_changed',
                            'payload': {'action': action},
                        }],
                        'relevant': True,
                        'events_truncated': False,
                        'journal': {'generation': 'epoch-1', 'max_version': 1},
                    },
                    1,
                )
            return 'caught_up', journal_status(), cursor

        async def collect():
            with (
                patch('core.sse._resolve_stream_context', side_effect=resolve),
                patch('core.sse.operational_journal_status', side_effect=journal_status),
                patch('core.sse._read_page', side_effect=read_page),
                override_settings(
                    SSE_STREAM_MAX_SECONDS=2,
                    SSE_REAUTH_SECONDS=10,
                    SSE_HEARTBEAT_SECONDS=10,
                    SSE_SAFETY_SCAN_SECONDS=1,
                    SSE_BATCH_SIZE=2,
                ),
            ):
                stream = operational_event_stream(
                    object(), initial_context=original, cursor=0,
                    subscriber_factory=Subscription,
                )
                try:
                    await stream.__anext__()
                    return await stream.__anext__()
                finally:
                    await stream.aclose()

        return asyncio.run(collect()).decode('utf-8')

    def test_haul_assignment_addition_replays_event_instead_of_scope_reset(self):
        frame = self._collect_dynamic_assignment_frame(
            before_ids=[10],
            after_ids=[10, 20],
            action='assignment_applied',
        )

        self.assertIn('event: operational', frame)
        self.assertIn('"type":"assignment_changed"', frame)
        self.assertIn('"action":"assignment_applied"', frame)
        self.assertNotIn('event: reset', frame)

    def test_haul_assignment_removal_replays_event_instead_of_scope_reset(self):
        frame = self._collect_dynamic_assignment_frame(
            before_ids=[10, 20],
            after_ids=[10],
            action='release_applied',
        )

        self.assertIn('event: operational', frame)
        self.assertIn('"type":"assignment_changed"', frame)
        self.assertIn('"action":"release_applied"', frame)
        self.assertNotIn('event: reset', frame)

    def test_lost_redis_wakeup_is_recovered_by_next_database_safety_scan(self):
        state = {'committed': False}
        context = self._stream_context()
        journal0 = self._journal()
        journal1 = self._journal(version=1)

        class SilentSubscription:
            async def connect(self):
                return True

            async def wait(self, _timeout):
                # The database commit happens, but Redis deliberately returns
                # no message. The next loop must still scan the DB.
                state['committed'] = True
                return None

            async def close(self):
                return None

        def journal_status():
            return journal1 if state['committed'] else journal0

        def read_page(_context, cursor, _batch_size):
            if state['committed'] and cursor == 0:
                payload = {
                    'version': 1,
                    'events': [{'version': 1, 'type': 'reference_changed'}],
                    'relevant': True,
                    'events_truncated': False,
                    'journal': {'generation': 'epoch-1', 'max_version': 1},
                }
                return 'page', payload, 1
            return 'caught_up', journal_status(), cursor

        async def collect():
            with (
                patch('core.sse._resolve_stream_context', return_value=context),
                patch('core.sse.operational_journal_status', side_effect=journal_status),
                patch('core.sse._read_page', side_effect=read_page),
                override_settings(
                    SSE_STREAM_MAX_SECONDS=2,
                    SSE_REAUTH_SECONDS=10,
                    SSE_HEARTBEAT_SECONDS=10,
                    SSE_SAFETY_SCAN_SECONDS=1,
                    SSE_BATCH_SIZE=2,
                ),
            ):
                stream = operational_event_stream(
                    object(), initial_context=context, cursor=0,
                    subscriber_factory=SilentSubscription,
                )
                try:
                    first = await stream.__anext__()
                    second = await stream.__anext__()
                    return first, second
                finally:
                    await stream.aclose()

        first, second = asyncio.run(collect())
        self.assertIn(b'event: heartbeat', first)
        self.assertIn(b'event: operational', second)
        self.assertIn(b'id: 1', second)

    def test_midstream_journal_epoch_change_forces_snapshot_reset(self):
        state = {'rotated': False}
        context = self._stream_context()

        class Subscription:
            async def connect(self):
                return True

            async def wait(self, _timeout):
                state['rotated'] = True
                return None

            async def close(self):
                return None

        def journal_status():
            return self._journal(epoch='epoch-2' if state['rotated'] else 'epoch-1')

        async def collect():
            with (
                patch('core.sse._resolve_stream_context', return_value=context),
                patch('core.sse.operational_journal_status', side_effect=journal_status),
                patch(
                    'core.sse._read_page',
                    side_effect=lambda _context, cursor, _batch: (
                        'caught_up', journal_status(), cursor
                    ),
                ),
                override_settings(
                    SSE_STREAM_MAX_SECONDS=2,
                    SSE_REAUTH_SECONDS=10,
                    SSE_HEARTBEAT_SECONDS=10,
                    SSE_SAFETY_SCAN_SECONDS=1,
                    SSE_BATCH_SIZE=2,
                ),
            ):
                stream = operational_event_stream(
                    object(), initial_context=context, cursor=0,
                    subscriber_factory=Subscription,
                )
                try:
                    await stream.__anext__()
                    return await stream.__anext__()
                finally:
                    await stream.aclose()

        reset = asyncio.run(collect())
        self.assertIn(b'event: reset', reset)
        self.assertIn(b'journal_epoch_changed', reset)

    def test_midstream_equipment_scope_change_forces_reset_before_scan(self):
        original = self._stream_context(scope='scope-1', equipment_ids=[10])
        changed = self._stream_context(scope='scope-2', equipment_ids=[10, 20])
        calls = {'resolve': 0}

        class Subscription:
            async def connect(self):
                return True

            async def wait(self, _timeout):
                return None

            async def close(self):
                return None

        def resolve(_request):
            calls['resolve'] += 1
            return original if calls['resolve'] == 1 else changed

        async def collect():
            journal = self._journal()
            with (
                patch('core.sse._resolve_stream_context', side_effect=resolve),
                patch('core.sse.operational_journal_status', return_value=journal),
                patch(
                    'core.sse._read_page',
                    return_value=('caught_up', journal, 0),
                ),
                override_settings(
                    SSE_STREAM_MAX_SECONDS=2,
                    SSE_REAUTH_SECONDS=10,
                    SSE_HEARTBEAT_SECONDS=10,
                    SSE_SAFETY_SCAN_SECONDS=1,
                    SSE_BATCH_SIZE=2,
                ),
            ):
                stream = operational_event_stream(
                    object(), initial_context=original, cursor=0,
                    subscriber_factory=Subscription,
                )
                try:
                    await stream.__anext__()
                    return await stream.__anext__()
                finally:
                    await stream.aclose()

        reset = asyncio.run(collect())
        self.assertIn(b'event: reset', reset)
        self.assertIn(b'role_context_changed', reset)

    def test_midstream_access_revocation_forces_unauthenticated_reset(self):
        context = self._stream_context()
        calls = {'resolve': 0}

        class Subscription:
            async def connect(self):
                return True

            async def wait(self, _timeout):
                return None

            async def close(self):
                return None

        def resolve(_request):
            calls['resolve'] += 1
            if calls['resolve'] > 1:
                raise StreamRejected('authentication_required', status=401)
            return context

        async def collect():
            journal = self._journal()
            with (
                patch('core.sse._resolve_stream_context', side_effect=resolve),
                patch('core.sse.operational_journal_status', return_value=journal),
                patch(
                    'core.sse._read_page',
                    return_value=('caught_up', journal, 0),
                ),
                override_settings(
                    SSE_STREAM_MAX_SECONDS=2,
                    SSE_REAUTH_SECONDS=10,
                    SSE_HEARTBEAT_SECONDS=10,
                    SSE_SAFETY_SCAN_SECONDS=1,
                    SSE_BATCH_SIZE=2,
                ),
            ):
                stream = operational_event_stream(
                    object(), initial_context=context, cursor=0,
                    subscriber_factory=Subscription,
                )
                try:
                    await stream.__anext__()
                    return await stream.__anext__()
                finally:
                    await stream.aclose()

        reset = asyncio.run(collect())
        self.assertIn(b'event: reset', reset)
        self.assertIn(b'authentication_required', reset)
        self.assertIn(b'"authenticated":false', reset)


@override_settings(
    SSE_PILOT_ENABLED=True,
    SSE_REDIS_URL='',
    SSE_HEARTBEAT_SECONDS=1,
    SSE_SAFETY_SCAN_SECONDS=1,
    SSE_REAUTH_SECONDS=1,
    SSE_STREAM_MAX_SECONDS=2,
    SSE_BATCH_SIZE=2,
    ROLE_APP_BASE_DOMAINS=('localhost',),
    ALLOWED_HOSTS=['localhost', 'testserver'],
)
class SseStreamDatabaseTests(_StreamConnectionCleanupMixin, TransactionTestCase):
    # NB: `reset_sequences = True` нельзя включать в этом классе. На PostgreSQL
    # Django сбрасывает КАЖДУЮ последовательность в 1
    # (django/db/backends/postgresql/operations.py:216-233), а тестовая база после
    # миграций уже содержит seed-строки (users_role 1..5, справочники). Первый же
    # Role.objects.create получает id=1 и падает на users_role_pkey. На SQLite
    # сброс последовательностей не поддерживается, поэтому там ошибки не было.
    # Тесты используют фактические pk, а не предположение «id начинается с 1».

    def setUp(self):
        self.employee = Employee.objects.create(
            full_name='Тестовый водитель SSE',
            phone='79000000551',
            is_active=True,
            status=Employee.Status.ACTIVE,
        )
        self.role, _ = Role.objects.get_or_create(
            code='driver',
            defaults={
                'name': 'Водитель SSE',
                'is_active': True,
            },
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.role,
            access_code='551551',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        OperationalStateEvent.objects.all().delete()
        state, _ = OperationalStateVersion.objects.get_or_create(key='production')
        state.version = 0
        state.reason = ''
        state.save(update_fields=['version', 'reason'])
        self.client = Client()
        session = self.client.session
        session['employee_access_id'] = self.access.id
        session.save()
        self.url = reverse('operational_state_stream')

    async def _next_chunk(self, response):
        iterator = response.streaming_content.__aiter__()
        try:
            return await iterator.__anext__()
        finally:
            await iterator.aclose()

    async def _next_chunks(self, response, count):
        iterator = response.streaming_content.__aiter__()
        chunks = []
        try:
            while len(chunks) < count:
                chunks.append(await iterator.__anext__())
            return chunks
        finally:
            await iterator.aclose()

    def _first_chunk(self, response):
        return asyncio.run(self._next_chunk(response)).decode('utf-8')

    def _set_events(self, events, version):
        OperationalStateEvent.objects.bulk_create(events)
        OperationalStateVersion.objects.filter(key='production').update(
            version=version,
            reason='test',
        )

    def _equipment(self, kind, number):
        equipment_type, _ = EquipmentType.objects.get_or_create(name=kind)
        return Equipment.objects.create(
            equipment_type=equipment_type,
            garage_number=number,
            is_active=True,
        )

    def _open_shift(self, employee, role_code, equipment):
        return EmployeeShift.objects.create(
            employee=employee,
            shift_type=ShiftType.DAY,
            workplace_code=role_code,
            equipment=equipment,
            opened_at=timezone.now(),
        )

    def _force_journal_gap(self, *, high_water=50, retained_version=40):
        OperationalStateEvent.objects.all().delete()
        OperationalStateEvent.objects.create(
            key='production',
            version=retained_version,
            event_type='reference_changed',
            payload={'action': 'retained_tail'},
        )
        OperationalStateVersion.objects.filter(key='production').update(
            version=high_water,
            reason='pruned-test-journal',
        )

    def test_long_lived_scan_connection_reopens_before_reading_external_commit(self):
        def scan():
            try:
                refresh_stream_database_snapshot()
                return operational_journal_status()['max_version']
            finally:
                # Соединение рабочего потока пула закрывается явно: при
                # CONN_MAX_AGE=60 close_old_connections() его не закрывает,
                # а Django в teardown закрывает только соединения текущего
                # потока (образец — T-021, users/test_session_revocation_postgresql.py).
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as executor:
            self.assertEqual(executor.submit(scan).result(), 0)
            self._set_events([
                OperationalStateEvent(
                    key='production',
                    version=1,
                    event_type='reference_changed',
                    payload={'action': 'external_commit'},
                ),
            ], 1)
            self.assertEqual(executor.submit(scan).result(), 1)

    def test_stream_replays_relevant_event_and_exposes_context(self):
        self._set_events([
            OperationalStateEvent(
                key='production',
                version=1,
                event_type='reference_changed',
                object_type='DumpPoint',
                object_id='3',
                payload={'action': 'save'},
            ),
        ], 1)

        response = self.client.get(
            self.url,
            {'after': 0, 'role_app_code': 'driver'},
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/event-stream; charset=utf-8')
        self.assertEqual(response['X-Accel-Buffering'], 'no')
        frame = self._first_chunk(response)
        self.assertIn('id: 1\n', frame)
        self.assertIn('event: operational\n', frame)
        payload = json.loads(next(
            line.removeprefix('data: ')
            for line in frame.splitlines()
            if line.startswith('data: ')
        ))
        self.assertEqual(payload['version'], 1)
        self.assertTrue(payload['page_final'])
        self.assertTrue(payload['caught_up'])
        self.assertEqual(payload['high_water_version'], 1)
        self.assertTrue(payload['relevant'])
        self.assertEqual(payload['events'][0]['type'], 'reference_changed')
        self.assertEqual(payload['role_app_code'], 'driver')
        self.assertEqual(payload['context']['access_id'], self.access.id)
        self.assertTrue(payload['journal']['generation'])

    def test_legacy_native_state_exposes_same_cursor_partition_metadata(self):
        response = self.client.get(
            reverse('operational_state_version'),
            {'after': 0, 'include_events': 0, 'role_app_code': 'driver'},
            HTTP_HOST='localhost',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['context']['access_id'], self.access.id)
        self.assertEqual(payload['context']['employee_id'], self.employee.id)
        self.assertEqual(payload['context']['role_app_code'], 'driver')
        self.assertTrue(payload['context']['cursor_scope'])
        self.assertEqual(payload['journal_epoch'], payload['journal']['generation'])
        self.assertEqual(payload['journal']['max_version'], payload['version'])
        self.assertEqual(payload['page_cursor'], 0)
        self.assertEqual(payload['scanned_through_version'], 0)
        self.assertEqual(payload['high_water_version'], 0)
        self.assertTrue(payload['caught_up'])

    def test_legacy_native_recovery_pages_past_foreign_events_to_final_cancellation(self):
        foreign_events = [
            OperationalStateEvent(
                key='production',
                version=version,
                event_type='assignment_changed',
                object_type='HaulAssignment',
                object_id=str(version),
                payload={
                    'action': 'assignment_applied',
                    'truck_ids': [900000 + version],
                },
            )
            for version in range(1, 1001)
        ]
        relevant_events = [
            OperationalStateEvent(
                key='production',
                version=1001,
                event_type='assignment_changed',
                object_type='HaulAssignment',
                object_id='voice-operation',
                payload={
                    'action': 'assignment_applied',
                    'employee_id': self.employee.id,
                },
            ),
            OperationalStateEvent(
                key='production',
                version=1002,
                event_type='assignment_changed',
                object_type='HaulAssignment',
                object_id='voice-operation',
                payload={
                    'action': 'release_applied',
                    'employee_id': self.employee.id,
                },
            ),
        ]
        self._set_events(foreign_events + relevant_events, 1002)

        first = self.client.get(
            reverse('operational_state_version'),
            {'after': 0, 'include_events': 1, 'role_app_code': 'driver'},
            HTTP_HOST='localhost',
        ).json()
        self.assertEqual(first['version'], 1002)
        self.assertEqual(first['events'], [])
        self.assertTrue(first['events_truncated'])
        self.assertEqual(first['page_cursor'], 1000)
        self.assertEqual(first['scanned_through_version'], 1000)
        self.assertEqual(first['high_water_version'], 1002)
        self.assertFalse(first['caught_up'])

        final = self.client.get(
            reverse('operational_state_version'),
            {
                'after': first['page_cursor'],
                'include_events': 1,
                'role_app_code': 'driver',
            },
            HTTP_HOST='localhost',
        ).json()
        self.assertEqual(
            [event['payload']['action'] for event in final['events']],
            ['assignment_applied', 'release_applied'],
        )
        self.assertFalse(final['events_truncated'])
        self.assertEqual(final['page_cursor'], 1002)
        self.assertEqual(final['scanned_through_version'], 1002)
        self.assertEqual(final['high_water_version'], 1002)
        self.assertTrue(final['caught_up'])

    def test_legacy_gap_reset_contains_authoritative_current_driver_assignment(self):
        truck = self._equipment('Самосвал voice truth', 'С-GAP-1')
        excavator = self._equipment('Экскаватор voice truth', 'Э-GAP-1')
        self._open_shift(self.employee, 'driver', truck)
        assignment = HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            action=HaulAssignmentAction.ASSIGN,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        self._force_journal_gap()

        payload = self.client.get(
            reverse('operational_state_version'),
            {'after': 0, 'include_events': 1, 'role_app_code': 'driver'},
            HTTP_HOST='localhost',
        ).json()

        self.assertTrue(payload['reset_required'])
        self.assertTrue(payload['snapshot_required'])
        self.assertEqual(payload['reset_reason'], 'journal_gap')
        self.assertEqual(payload['reset_to_version'], 50)
        self.assertEqual(payload['page_cursor'], 0)
        self.assertFalse(payload['caught_up'])
        self.assertEqual(payload['events'], [])
        truth = payload['current_voice_state']
        self.assertTrue(truth['authoritative'])
        self.assertEqual(truth['source'], 'domain_tables')
        self.assertEqual(truth['observed_at_version'], 50)
        self.assertEqual(
            truth['voice_operations'],
            [{
                'operation_key': f'assign:{assignment.id}',
                'kind': 'driver_assignment',
                'observed_at_version': 50,
                'assignment_id': assignment.id,
                'truck_id': truck.id,
                'truck_number': truck.garage_number,
                'excavator_id': excavator.id,
                'excavator_number': excavator.garage_number,
                'assignment_state': 'active',
            }],
        )

    def test_gap_truth_suppresses_cancelled_assignment_and_keeps_replacement(self):
        truck = self._equipment('Самосвал replacement truth', 'С-GAP-2')
        old_excavator = self._equipment('Экскаватор old truth', 'Э-GAP-OLD')
        replacement_excavator = self._equipment(
            'Экскаватор replacement truth',
            'Э-GAP-NEW',
        )
        self._open_shift(self.employee, 'driver', truck)
        old = HaulAssignment.objects.create(
            truck=truck,
            excavator=old_excavator,
            action=HaulAssignmentAction.ASSIGN,
            status=AssignmentStatus.CANCELLED,
            ended_at=timezone.now(),
        )
        replacement = HaulAssignment.objects.create(
            truck=truck,
            excavator=replacement_excavator,
            action=HaulAssignmentAction.ASSIGN,
            status=AssignmentStatus.PENDING,
            # Назначение ещё не наступило. Экран состояния сам применяет
            # «созревшие» PENDING (core/views.py:199-201 →
            # reconcile_due_haul_assignments_throttled, окно 5 с), поэтому
            # effective_at=now сделал бы результат зависимым от того, успел ли
            # прошлый запрос съесть окно троттлинга. Тест проверяет именно
            # будущее (ещё не применённое) назначение: замена не потеряна, но
            # и не выдаётся за активную.
            effective_at=timezone.now() + timezone.timedelta(hours=1),
        )
        self._force_journal_gap()

        payload = self.client.get(
            reverse('operational_state_version'),
            {'after': 0, 'role_app_code': 'driver'},
            HTTP_HOST='localhost',
        ).json()
        operations = payload['current_voice_state']['voice_operations']
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]['operation_key'], f'assign:{replacement.id}')
        self.assertEqual(operations[0]['assignment_state'], 'pending')
        self.assertNotIn(old.id, [item['assignment_id'] for item in operations])
        truck_state = payload['current_voice_state']['driver']['truck_states'][0]
        self.assertEqual(truck_state['current_assignment']['assignment_id'], replacement.id)

    def test_gap_truth_excludes_stale_trip_and_keeps_current_loaded_destination(self):
        truck = self._equipment('Самосвал trip truth', 'С-GAP-3')
        excavator = self._equipment('Экскаватор trip truth', 'Э-GAP-3')
        self._open_shift(self.employee, 'driver', truck)
        rock = RockType.objects.create(name='Порода gap truth')
        stale_point = DumpPoint.objects.create(name='Старый отвал gap truth')
        current_point = DumpPoint.objects.create(name='Текущий отвал gap truth')
        stale = Trip.objects.create(
            truck=truck,
            excavator=excavator,
            driver=self.employee,
            rock_type=rock,
            dump_point=stale_point,
            assigned_dump_point=stale_point,
            status=TripStatus.CANCELLED,
            loaded_at=timezone.now(),
        )
        current = Trip.objects.create(
            truck=truck,
            excavator=excavator,
            driver=self.employee,
            rock_type=rock,
            dump_point=current_point,
            assigned_dump_point=current_point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=timezone.now(),
        )
        self._force_journal_gap()

        payload = self.client.get(
            reverse('operational_state_version'),
            {'after': 0, 'role_app_code': 'driver'},
            HTTP_HOST='localhost',
        ).json()
        dump_operations = [
            item for item in payload['current_voice_state']['voice_operations']
            if item['kind'] == 'driver_dump_point'
        ]
        self.assertEqual(len(dump_operations), 1)
        self.assertEqual(dump_operations[0]['trip_id'], current.id)
        self.assertEqual(dump_operations[0]['dump_point_id'], current_point.id)
        self.assertNotIn(stale.id, [item['trip_id'] for item in dump_operations])

    def test_gap_truth_excludes_loaded_trip_owned_by_previous_driver_shift(self):
        truck = self._equipment('Самосвал previous driver truth', 'С-GAP-PREV')
        excavator = self._equipment('Экскаватор previous driver truth', 'Э-GAP-PREV')
        point = DumpPoint.objects.create(name='Отвал previous driver truth')
        rock = RockType.objects.create(name='Порода previous driver truth')
        previous_driver = Employee.objects.create(
            full_name='Предыдущий водитель SSE',
            phone='79000000553',
            is_active=True,
            status=Employee.Status.ACTIVE,
        )
        previous_shift = EmployeeShift.objects.create(
            employee=previous_driver,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            equipment=truck,
            opened_at=timezone.now() - timezone.timedelta(hours=2),
            closed_at=timezone.now() - timezone.timedelta(hours=1),
        )
        previous_trip = Trip.objects.create(
            truck=truck,
            excavator=excavator,
            driver=previous_driver,
            driver_participation_recorded=True,
            driver_control_shift=previous_shift,
            rock_type=rock,
            dump_point=point,
            assigned_dump_point=point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=timezone.now(),
        )
        self._open_shift(self.employee, 'driver', truck)
        self._force_journal_gap()

        payload = self.client.get(
            reverse('operational_state_version'),
            {'after': 0, 'role_app_code': 'driver'},
            HTTP_HOST='localhost',
        ).json()
        dump_operations = [
            item for item in payload['current_voice_state']['voice_operations']
            if item['kind'] == 'driver_dump_point'
        ]
        self.assertEqual(dump_operations, [])
        self.assertNotIn(
            previous_trip.id,
            [
                state['loaded_trip']['trip_id']
                for state in payload['current_voice_state']['driver']['truck_states']
                if state['loaded_trip']
            ],
        )

    def test_gap_truth_keeps_only_trip_owned_by_current_driver_control_shift(self):
        truck = self._equipment('Самосвал current control truth', 'С-GAP-CUR')
        other_truck = self._equipment('Самосвал old employee truth', 'С-GAP-OLD-SELF')
        excavator = self._equipment('Экскаватор current control truth', 'Э-GAP-CUR')
        point = DumpPoint.objects.create(name='Отвал current control truth')
        rock = RockType.objects.create(name='Порода current control truth')
        old_shift = EmployeeShift.objects.create(
            employee=self.employee,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            equipment=other_truck,
            opened_at=timezone.now() - timezone.timedelta(hours=3),
            closed_at=timezone.now() - timezone.timedelta(hours=2),
        )
        old_trip = Trip.objects.create(
            truck=other_truck,
            excavator=excavator,
            driver=self.employee,
            driver_participation_recorded=True,
            driver_control_shift=old_shift,
            rock_type=rock,
            dump_point=point,
            assigned_dump_point=point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=timezone.now(),
        )
        current_shift = self._open_shift(self.employee, 'driver', truck)
        current_trip = Trip.objects.create(
            truck=truck,
            excavator=excavator,
            driver=self.employee,
            driver_participation_recorded=True,
            driver_control_shift=current_shift,
            rock_type=rock,
            dump_point=point,
            assigned_dump_point=point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=timezone.now(),
        )
        self._force_journal_gap()

        payload = self.client.get(
            reverse('operational_state_version'),
            {'after': 0, 'role_app_code': 'driver'},
            HTTP_HOST='localhost',
        ).json()
        dump_operations = [
            item for item in payload['current_voice_state']['voice_operations']
            if item['kind'] == 'driver_dump_point'
        ]
        self.assertEqual(
            [item['trip_id'] for item in dump_operations],
            [current_trip.id],
        )
        self.assertNotIn(old_trip.id, [item['trip_id'] for item in dump_operations])

    def test_gap_truth_uses_same_legacy_driver_shift_rule_as_work_screen(self):
        truck = self._equipment('Самосвал', 'С-GAP-LEGACY')
        excavator = self._equipment('Экскаватор legacy truth', 'Э-GAP-LEGACY')
        point = DumpPoint.objects.create(name='Отвал legacy driver truth')
        rock = RockType.objects.create(name='Порода legacy driver truth')
        legacy_shift = EmployeeShift.objects.create(
            employee=self.employee,
            shift_type=ShiftType.DAY,
            workplace_code='',
            equipment=truck,
            opened_at=timezone.now(),
        )
        EquipmentAssignment.objects.create(
            employee=self.employee,
            role=self.role,
            equipment=truck,
            shift_type=ShiftType.DAY,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        trip = Trip.objects.create(
            truck=truck,
            excavator=excavator,
            driver=self.employee,
            driver_participation_recorded=True,
            driver_control_shift=legacy_shift,
            rock_type=rock,
            dump_point=point,
            assigned_dump_point=point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=timezone.now(),
        )
        self._force_journal_gap()

        payload = self.client.get(
            reverse('operational_state_version'),
            {'after': 0, 'role_app_code': 'driver'},
            HTTP_HOST='localhost',
        ).json()
        dump_operations = [
            item for item in payload['current_voice_state']['voice_operations']
            if item['kind'] == 'driver_dump_point'
        ]
        self.assertEqual([item['trip_id'] for item in dump_operations], [trip.id])

    def test_gap_truth_pending_release_suppresses_active_driver_assignment_voice(self):
        truck = self._equipment('Самосвал release truth', 'С-GAP-REL')
        excavator = self._equipment('Экскаватор release truth', 'Э-GAP-REL')
        self._open_shift(self.employee, 'driver', truck)
        active = HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            action=HaulAssignmentAction.ASSIGN,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        release = HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            action=HaulAssignmentAction.RELEASE,
            status=AssignmentStatus.PENDING,
            # Снятие ещё не наступило: экран состояния применяет созревшие
            # PENDING сам (core/views.py:199-201), и с effective_at=now тест
            # зависел бы от 5-секундного окна троттлинга
            # (assignments/services.py:1919-1949) — состояние становилось бы
            # 'unassigned' вместо проверяемого 'release_pending'.
            effective_at=timezone.now() + timezone.timedelta(hours=1),
        )
        self._force_journal_gap()

        payload = self.client.get(
            reverse('operational_state_version'),
            {'after': 0, 'role_app_code': 'driver'},
            HTTP_HOST='localhost',
        ).json()
        truth = payload['current_voice_state']
        self.assertEqual(truth['voice_operations'], [])
        truck_state = truth['driver']['truck_states'][0]
        self.assertEqual(truck_state['assignment_state'], 'release_pending')
        self.assertIsNone(truck_state['current_assignment'])
        self.assertEqual(truck_state['active_assignment']['assignment_id'], active.id)
        self.assertEqual(truck_state['pending_release']['assignment_id'], release.id)

    def test_legacy_cursor_ahead_uses_same_authoritative_reset_branch(self):
        payload = self.client.get(
            reverse('operational_state_version'),
            {'after': 9, 'role_app_code': 'driver'},
            HTTP_HOST='localhost',
        ).json()

        self.assertTrue(payload['reset_required'])
        self.assertTrue(payload['snapshot_required'])
        self.assertEqual(payload['reset_reason'], 'cursor_ahead')
        self.assertEqual(payload['reset_to_version'], 0)
        self.assertEqual(payload['page_cursor'], 9)
        self.assertEqual(payload['scanned_through_version'], 9)
        self.assertEqual(payload['high_water_version'], 0)
        self.assertFalse(payload['caught_up'])
        self.assertEqual(payload['events'], [])
        self.assertTrue(payload['current_voice_state']['authoritative'])

    def test_excavator_gap_truth_contains_only_current_accepted_trucks(self):
        excavator_role, _ = Role.objects.get_or_create(
            code='excavator_operator',
            defaults={
                'name': 'Машинист SSE gap',
                'is_active': True,
            },
        )
        operator = Employee.objects.create(
            full_name='Машинист SSE gap',
            phone='79000000552',
            is_active=True,
            status=Employee.Status.ACTIVE,
        )
        access = EmployeeAccess.objects.create(
            employee=operator,
            role=excavator_role,
            access_code='552552',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        excavator = self._equipment('Экскаватор operator truth', 'Э-GAP-4')
        truck = self._equipment('Самосвал operator truth', 'С-GAP-4')
        self._open_shift(operator, 'excavator_operator', excavator)
        assignment = HaulAssignment.objects.create(
            truck=truck,
            excavator=excavator,
            action=HaulAssignmentAction.ASSIGN,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        self._force_journal_gap()
        client = Client()
        session = client.session
        session['employee_access_id'] = access.id
        session.save()

        payload = client.get(
            reverse('operational_state_version'),
            {'after': 0, 'role_app_code': 'excavator_operator'},
            HTTP_HOST='localhost',
        ).json()
        truth = payload['current_voice_state']
        self.assertIsNone(truth['driver'])
        self.assertEqual(
            truth['voice_operations'][0]['operation_key'],
            f'excavator-assignment:assign:{assignment.id}:{excavator.id}',
        )
        self.assertEqual(
            truth['excavator']['equipment_states'][0]['assignments'][0]['truck_id'],
            truck.id,
        )

    def test_irrelevant_page_still_advances_global_cursor(self):
        self._set_events([
            OperationalStateEvent(
                key='production',
                version=1,
                event_type='assignment_changed',
                object_type='HaulAssignment',
                object_id='99',
                payload={'action': 'save', 'equipment_id': 999999},
            ),
        ], 1)

        response = self.client.get(
            self.url,
            {'after': 0, 'role_app_code': 'driver'},
            HTTP_HOST='localhost',
        )
        frame = self._first_chunk(response)
        payload = json.loads(next(
            line.removeprefix('data: ')
            for line in frame.splitlines()
            if line.startswith('data: ')
        ))
        self.assertIn('id: 1\n', frame)
        self.assertFalse(payload['relevant'])
        self.assertEqual(payload['events'], [])

    def test_paged_backlog_marks_only_high_water_page_final(self):
        self._set_events([
            OperationalStateEvent(
                key='production',
                version=version,
                event_type='reference_changed',
                object_type='DumpPoint',
                object_id=str(version),
                payload={'action': 'save', 'sequence': version},
            )
            for version in (1, 2, 3)
        ], 3)
        response = self.client.get(
            self.url,
            {'after': 0, 'role_app_code': 'driver'},
            HTTP_HOST='localhost',
        )
        frames = [
            chunk.decode('utf-8')
            for chunk in asyncio.run(self._next_chunks(response, 2))
        ]
        payloads = [
            json.loads(next(
                line.removeprefix('data: ')
                for line in frame.splitlines()
                if line.startswith('data: ')
            ))
            for frame in frames
        ]
        self.assertEqual([payload['version'] for payload in payloads], [2, 3])
        self.assertEqual([payload['page_final'] for payload in payloads], [False, True])
        self.assertTrue(all(payload['high_water_version'] == 3 for payload in payloads))

    def test_missing_journal_range_emits_reset_and_closes(self):
        self._set_events([
            OperationalStateEvent(
                key='production',
                version=3,
                event_type='reference_changed',
                payload={'action': 'save'},
            ),
        ], 3)

        response = self.client.get(
            self.url,
            {'after': 0, 'role_app_code': 'driver'},
            HTTP_HOST='localhost',
        )
        frame = self._first_chunk(response)
        self.assertIn('event: reset\n', frame)
        payload = json.loads(next(
            line.removeprefix('data: ')
            for line in frame.splitlines()
            if line.startswith('data: ')
        ))
        self.assertEqual(payload['reason'], 'journal_gap')
        self.assertTrue(payload['snapshot_required'])
        self.assertIn('_operational_snapshot=1', payload['snapshot_url'])

    def test_stream_rejects_revoked_access_before_headers(self):
        self.access.is_active = False
        self.access.save(update_fields=['is_active'])
        response = self.client.get(
            self.url,
            {'after': 0, 'role_app_code': 'driver'},
            HTTP_HOST='localhost',
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error'], 'authentication_required')

    # --- T-016 WP-2: replay / idempotency contract -------------------------

    def _payload_from_frame(self, frame):
        return json.loads(next(
            line.removeprefix('data: ')
            for line in frame.splitlines()
            if line.startswith('data: ')
        ))

    def _stream_page(self, params=None, **extra):
        response = self.client.get(
            self.url,
            params or {'after': 0, 'role_app_code': 'driver'},
            HTTP_HOST='localhost',
            **extra,
        )
        self.assertEqual(response.status_code, 200)
        return response, self._first_chunk(response)

    def _dump_point_change(self, version, trip, truck, previous_point, point):
        return OperationalStateEvent(
            key='production',
            version=version,
            event_type='trip_changed',
            object_type='Trip',
            object_id=str(trip.pk),
            payload={
                'action': 'change_actual_unload_point',
                'trip_id': trip.pk,
                'truck_id': truck.pk,
                'excavator_id': trip.excavator_id,
                'previous_dump_point_id': previous_point.pk,
                'actual_dump_point_id': point.pk,
                'occurred_at': timezone.now().isoformat(),
            },
        )

    def test_redelivered_page_keeps_the_same_event_id_and_does_not_bump_state(self):
        """A reconnect is allowed to re-send the same frame id, not a new fact."""
        truck = self._equipment('Самосвал replay id', 'С-REPLAY-1')
        self._open_shift(self.employee, 'driver', truck)
        # Fixture saves already bumped the journal through model signals.
        base = OperationalStateVersion.objects.get(key='production').version
        self._set_events([
            OperationalStateEvent(
                key='production',
                version=base + 1,
                event_type='trip_changed',
                object_type='Trip',
                object_id='91',
                payload={'action': 'trip_loaded', 'truck_id': truck.pk},
            ),
        ], base + 1)

        first_response, first_frame = self._stream_page(
            {'after': base, 'role_app_code': 'driver'},
        )
        second_response, second_frame = self._stream_page(
            {'after': base, 'role_app_code': 'driver'},
        )

        self.assertIn(f'id: {base + 1}\n', first_frame)
        self.assertIn(f'id: {base + 1}\n', second_frame)
        self.assertNotEqual(id(first_response), id(second_response))
        self.assertEqual(
            self._payload_from_frame(first_frame)['events'][0]['version'],
            base + 1,
        )
        self.assertEqual(
            self._payload_from_frame(second_frame)['events'][0]['version'],
            base + 1,
        )
        # Delivery is read-only: no server-side business effect is repeated.
        self.assertEqual(
            OperationalStateVersion.objects.get(key='production').version,
            base + 1,
        )
        self.assertEqual(
            OperationalStateEvent.objects.filter(key='production').count(),
            base + 1,
        )

        # The cursor that acknowledges the delivered event must not re-deliver it.
        caught_up_response, caught_up_frame = self._stream_page(
            {'after': base + 1, 'role_app_code': 'driver'},
        )
        self.assertIn('event: heartbeat\n', caught_up_frame)
        self.assertNotIn('event: operational\n', caught_up_frame)
        self.assertEqual(
            self._payload_from_frame(caught_up_frame)['version'], base + 1,
        )
        self.assertEqual(
            caught_up_response['X-SSE-Cursor-Source'], 'after',
        )

    def test_applied_cursor_suppresses_replay_of_event_source_last_event_id(self):
        """EventSource re-sends Last-Event-ID after a drop; ``applied`` wins."""
        base = OperationalStateVersion.objects.get(key='production').version
        self._set_events([
            OperationalStateEvent(
                key='production',
                version=base + 1,
                event_type='reference_changed',
                object_type='DumpPoint',
                object_id='1',
                payload={'action': 'save'},
            ),
        ], base + 1)

        response, frame = self._stream_page(
            {'applied': base + 1, 'after': base, 'role_app_code': 'driver'},
            HTTP_LAST_EVENT_ID=str(base),
        )

        self.assertEqual(response['X-SSE-Cursor-Source'], 'applied')
        self.assertIn('event: heartbeat\n', frame)
        self.assertNotIn('event: operational\n', frame)
        self.assertNotIn(f'id: {base + 1}\n', frame)
        self.assertEqual(self._payload_from_frame(frame)['events'], [])

        # The same stale Last-Event-ID without an applied cursor still replays
        # the event, so a drop with no local progress cannot lose it.
        replay_response, replay_frame = self._stream_page(
            {'role_app_code': 'driver'},
            HTTP_LAST_EVENT_ID=str(base),
        )
        self.assertEqual(replay_response['X-SSE-Cursor-Source'], 'last_event_id')
        self.assertIn(f'id: {base + 1}\n', replay_frame)
        self.assertEqual(
            self._payload_from_frame(replay_frame)['events'][0]['version'],
            base + 1,
        )

    def test_two_dump_point_changes_in_one_trip_are_both_delivered(self):
        truck = self._equipment('Самосвал dump replay', 'С-DUMP-REPLAY')
        excavator = self._equipment('Экскаватор dump replay', 'Э-DUMP-REPLAY')
        self._open_shift(self.employee, 'driver', truck)
        rock = RockType.objects.create(name='Порода dump replay')
        first_point = DumpPoint.objects.create(name='Отвал dump replay 1')
        second_point = DumpPoint.objects.create(name='Отвал dump replay 2')
        third_point = DumpPoint.objects.create(name='Отвал dump replay 3')
        trip = Trip.objects.create(
            truck=truck,
            excavator=excavator,
            driver=self.employee,
            rock_type=rock,
            dump_point=first_point,
            assigned_dump_point=first_point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=timezone.now(),
        )
        base = OperationalStateVersion.objects.get(key='production').version
        self._set_events([
            self._dump_point_change(base + 1, trip, truck, first_point, second_point),
            self._dump_point_change(base + 2, trip, truck, second_point, third_point),
        ], base + 2)

        _, frame = self._stream_page(
            {'after': base, 'role_app_code': 'driver'},
        )

        payload = self._payload_from_frame(frame)
        self.assertEqual(
            [event['version'] for event in payload['events']],
            [base + 1, base + 2],
        )
        self.assertEqual(
            [event['payload']['actual_dump_point_id'] for event in payload['events']],
            [second_point.pk, third_point.pk],
        )
        self.assertTrue(payload['page_final'])
        self.assertEqual(payload['version'], base + 2)

    def test_distinct_relevant_events_for_the_same_worker_are_both_delivered(self):
        truck = self._equipment('Самосвал distinct events', 'С-DISTINCT')
        self._open_shift(self.employee, 'driver', truck)
        base = OperationalStateVersion.objects.get(key='production').version
        self._set_events([
            OperationalStateEvent(
                key='production',
                version=base + 1,
                event_type='trip_changed',
                object_type='Trip',
                object_id='71',
                payload={'action': 'trip_loaded', 'truck_id': truck.pk},
            ),
            OperationalStateEvent(
                key='production',
                version=base + 2,
                event_type='assignment_changed',
                object_type='HaulAssignment',
                object_id='72',
                payload={'action': 'assignment_applied', 'truck_id': truck.pk},
            ),
        ], base + 2)

        _, frame = self._stream_page(
            {'after': base, 'role_app_code': 'driver'},
        )

        payload = self._payload_from_frame(frame)
        self.assertEqual(
            [(event['type'], event['version']) for event in payload['events']],
            [('trip_changed', base + 1), ('assignment_changed', base + 2)],
        )

    def test_identical_duplicate_frames_collapse_but_cursor_reaches_high_water(self):
        """Only identical payloads collapse; the cursor still moves past them."""
        truck = self._equipment('Самосвал duplicate frames', 'С-DUPLICATE')
        self._open_shift(self.employee, 'driver', truck)
        base = OperationalStateVersion.objects.get(key='production').version
        duplicate_payload = {'action': 'trip_loaded', 'truck_id': truck.pk}
        self._set_events([
            OperationalStateEvent(
                key='production',
                version=base + 1,
                event_type='trip_changed',
                object_type='Trip',
                object_id='81',
                payload=duplicate_payload,
            ),
            OperationalStateEvent(
                key='production',
                version=base + 2,
                event_type='trip_changed',
                object_type='Trip',
                object_id='81',
                payload=duplicate_payload,
            ),
        ], base + 2)

        _, frame = self._stream_page(
            {'after': base, 'role_app_code': 'driver'},
        )

        payload = self._payload_from_frame(frame)
        self.assertEqual(len(payload['events']), 1)
        self.assertEqual(payload['events'][0]['version'], base + 1)
        self.assertEqual(payload['version'], base + 2)
        self.assertTrue(payload['caught_up'])
        self.assertIn(f'id: {base + 2}\n', frame)


class SseDisabledAndCommitTests(TestCase):
    @override_settings(SSE_PILOT_ENABLED=False)
    def test_sse_endpoint_is_disabled_by_default_contract(self):
        response = self.client.get(reverse('operational_state_stream'))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()['sse_enabled'])

    @override_settings(SSE_PILOT_ENABLED=True)
    @patch('core.sse_wakeup.publish_operational_wakeup')
    def test_redis_wakeup_runs_only_after_successful_commit(self, publish):
        with self.captureOnCommitCallbacks(execute=True):
            state = bump_operational_state('sse-test')
        publish.assert_called_once_with(state.version)

        publish.reset_mock()
        try:
            with self.captureOnCommitCallbacks(execute=True):
                from django.db import transaction

                with transaction.atomic():
                    bump_operational_state('rolled-back-sse-test')
                    raise RuntimeError('rollback')
        except RuntimeError:
            pass
        publish.assert_not_called()

    def test_event_version_is_unique_per_journal(self):
        OperationalStateEvent.objects.create(
            key='production', version=999, event_type='state_changed'
        )
        with self.assertRaises(IntegrityError):
            OperationalStateEvent.objects.create(
                key='production', version=999, event_type='state_changed'
            )


class ReconciliationCommandTests(SimpleTestCase):
    @patch('trips.manual_loading.reconcile_expired_manual_trips_throttled', return_value=[])
    @patch('assignments.services.reconcile_due_haul_assignments_throttled', return_value=0)
    def test_once_runs_both_existing_locked_reconcilers(self, assignments, manual):
        call_command('run_operational_reconciliation', '--once', verbosity=0)
        assignments.assert_called_once_with()
        manual.assert_called_once_with()
