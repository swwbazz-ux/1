"""T-031 B3b: вахтенная смена делопроизводителя — сервисы и HTTP."""

import json
from datetime import timedelta
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from shifts.models import WatchPeriod
from users.models import Employee, EmployeeAccess, Role

from .clerk_shifts import (
    clerk_shift_state,
    close_clerk_shift,
    handover_clerk_shift,
    open_clerk_shift,
)
from .control import (
    SettlementControlWriteContext,
    acquire_control_lease,
    lock_settlement_write_lease,
)
from .models import SettlementClerkShift, SettlementControlEvent, SettlementControlLease


def _codes(error):
    if hasattr(error, 'error_dict'):
        return sorted(
            item.code
            for items in error.error_dict.values()
            for item in items
        )
    return [item.code for item in error.error_list]


class ClerkShiftBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('load_physical_fund', stdout=StringIO())
        cls.clerk_role = Role.objects.create(
            code='settlement_clerk',
            name='Делопроизводитель',
        )
        cls.admin_role = Role.objects.create(code='admin', name='Администратор')
        cls.driver_role = Role.objects.create(code='driver', name='Водитель')
        cls.clerk = Employee.objects.create(
            full_name='Первый делопроизводитель',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.second_clerk = Employee.objects.create(
            full_name='Второй делопроизводитель',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.admin = Employee.objects.create(
            full_name='Администратор расселения',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.driver = Employee.objects.create(
            full_name='Водитель без смены',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.clerk_access = EmployeeAccess.objects.create(
            employee=cls.clerk,
            role=cls.clerk_role,
            access_code='SHIFT-CLERK-1',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.second_clerk_access = EmployeeAccess.objects.create(
            employee=cls.second_clerk,
            role=cls.clerk_role,
            access_code='SHIFT-CLERK-2',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.admin_access = EmployeeAccess.objects.create(
            employee=cls.admin,
            role=cls.admin_role,
            access_code='SHIFT-ADMIN',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.driver_access = EmployeeAccess.objects.create(
            employee=cls.driver,
            role=cls.driver_role,
            access_code='SHIFT-DRIVER',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        today = timezone.localdate()
        cls.watch_period = WatchPeriod.objects.create(
            name='Текущая вахта смены',
            starts_on=today - timedelta(days=1),
            ends_on=today + timedelta(days=1),
            is_active=True,
        )
        cls.old_watch_period = WatchPeriod.objects.create(
            name='Прошлая вахта смены',
            starts_on=today - timedelta(days=30),
            ends_on=today - timedelta(days=10),
            is_active=True,
        )
        cls.future_watch_period = WatchPeriod.objects.create(
            name='Будущая вахта смены',
            starts_on=today + timedelta(days=10),
            ends_on=today + timedelta(days=20),
            is_active=True,
        )

    def authenticate(self, access=None, client=None):
        client = client or self.client
        session = client.session
        session['employee_access_id'] = (access or self.clerk_access).pk
        session.save()
        return client

    def post_json(self, url_name, payload=None, client=None):
        return (client or self.client).post(
            reverse(url_name),
            data=payload or {},
            content_type='application/json',
        )

    def control_context(self, access, raw_session_key):
        grant = acquire_control_lease(
            owner_access_id=access.pk,
            raw_session_key=raw_session_key,
            source='clerk-shift-test',
        )
        return SettlementControlWriteContext(
            owner_access_id=access.pk,
            raw_session_key=raw_session_key,
            lease_token=str(grant.lease_token),
            fencing_revision=grant.fencing_revision,
        )

    def assert_no_store(self, response):
        self.assertEqual(response['Cache-Control'], 'private, no-store')


class ClerkShiftServiceTests(ClerkShiftBase):
    def test_open_shift_creates_open_shift_for_active_watch(self):
        shift = open_clerk_shift(access_id=self.clerk_access.pk)

        self.assertEqual(shift.status, SettlementClerkShift.Status.OPEN)
        self.assertEqual(shift.watch_period_id, self.watch_period.pk)
        self.assertEqual(shift.clerk_access_id, self.clerk_access.pk)
        self.assertIsNone(shift.closed_at)
        self.assertEqual(shift.close_kind, SettlementClerkShift.CloseKind.NORMAL)
        self.assertEqual(
            SettlementClerkShift.objects.filter(
                status=SettlementClerkShift.Status.OPEN,
            ).count(),
            1,
        )

    def test_open_shift_is_idempotent_for_same_access(self):
        first = open_clerk_shift(access_id=self.clerk_access.pk)
        second = open_clerk_shift(access_id=self.clerk_access.pk)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SettlementClerkShift.objects.count(), 1)

    def test_second_clerk_cannot_open_shift_on_same_watch(self):
        open_clerk_shift(access_id=self.clerk_access.pk)

        with self.assertRaises(ValidationError) as captured:
            open_clerk_shift(access_id=self.second_clerk_access.pk)

        self.assertEqual(_codes(captured.exception), ['settlement.shift.taken'])
        self.assertEqual(SettlementClerkShift.objects.count(), 1)

    def test_open_requires_active_watch(self):
        WatchPeriod.objects.filter(pk=self.watch_period.pk).update(is_active=False)

        with self.assertRaises(ValidationError) as captured:
            open_clerk_shift(access_id=self.clerk_access.pk)

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.shift.watch_not_active'],
        )
        self.assertFalse(SettlementClerkShift.objects.exists())

    def test_open_rejects_previous_watch_open_shift(self):
        SettlementClerkShift.objects.create(
            clerk_access=self.clerk_access,
            watch_period=self.old_watch_period,
            status=SettlementClerkShift.Status.OPEN,
            opened_at=timezone.now() - timedelta(days=20),
        )

        with self.assertRaises(ValidationError) as captured:
            open_clerk_shift(access_id=self.clerk_access.pk)

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.shift.previous_watch_open'],
        )

    def test_close_own_shift_and_repeat_is_idempotent(self):
        shift = open_clerk_shift(access_id=self.clerk_access.pk)

        closed = close_clerk_shift(access_id=self.clerk_access.pk)
        repeated = close_clerk_shift(access_id=self.clerk_access.pk)

        self.assertEqual(closed.status, SettlementClerkShift.Status.CLOSED)
        self.assertEqual(closed.closed_by_access_id, self.clerk_access.pk)
        self.assertIsNotNone(closed.closed_at)
        self.assertEqual(closed.close_kind, SettlementClerkShift.CloseKind.NORMAL)
        self.assertEqual(repeated.pk, shift.pk)
        self.assertEqual(repeated.status, SettlementClerkShift.Status.CLOSED)
        self.assertEqual(SettlementClerkShift.objects.count(), 1)

    def test_close_own_shift_ignores_client_close_kind(self):
        open_clerk_shift(access_id=self.clerk_access.pk)

        closed = close_clerk_shift(
            access_id=self.clerk_access.pk,
            reason='Подмена вида',
        )

        self.assertEqual(closed.close_kind, SettlementClerkShift.CloseKind.NORMAL)
        self.assertEqual(closed.close_reason, 'Подмена вида')

    def test_close_own_shift_releases_only_own_lease(self):
        open_clerk_shift(access_id=self.clerk_access.pk)
        self.control_context(self.clerk_access, 'own-closing-session')

        close_clerk_shift(access_id=self.clerk_access.pk)

        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertIsNone(lease.owner_access_id)

    def test_close_own_shift_keeps_parallel_admin_lease(self):
        open_clerk_shift(access_id=self.clerk_access.pk)
        self.control_context(self.admin_access, 'admin-session')

        close_clerk_shift(access_id=self.clerk_access.pk)

        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.owner_access_id, self.admin_access.pk)
        with transaction.atomic():
            still_admin = lock_settlement_write_lease(
                control_context=SettlementControlWriteContext(
                    owner_access_id=self.admin_access.pk,
                    raw_session_key='admin-session',
                    lease_token=str(lease.lease_token),
                    fencing_revision=lease.fencing_revision,
                ),
                using='default',
            )
        self.assertEqual(still_admin.owner_access_id, self.admin_access.pk)

    def test_closed_foreign_shift_is_hidden_from_other_clerk(self):
        shift = open_clerk_shift(access_id=self.clerk_access.pk)
        close_clerk_shift(access_id=self.clerk_access.pk)

        with self.assertRaises(ValidationError) as captured:
            close_clerk_shift(
                access_id=self.second_clerk_access.pk,
                shift_id=shift.pk,
            )
        self.assertEqual(_codes(captured.exception), ['settlement.shift.not_mine'])

        admin_view = close_clerk_shift(
            access_id=self.admin_access.pk,
            shift_id=shift.pk,
        )
        self.assertEqual(admin_view.pk, shift.pk)
        self.assertEqual(admin_view.status, SettlementClerkShift.Status.CLOSED)

    def test_clerk_cannot_close_foreign_shift_and_admin_can_with_reason(self):
        open_clerk_shift(access_id=self.clerk_access.pk)

        with self.assertRaises(ValidationError) as captured:
            close_clerk_shift(
                access_id=self.second_clerk_access.pk,
                reason='Хочу закрыть чужую',
            )
        self.assertEqual(_codes(captured.exception), ['settlement.shift.not_mine'])

        with self.assertRaises(ValidationError) as no_reason:
            close_clerk_shift(access_id=self.admin_access.pk)
        self.assertEqual(
            _codes(no_reason.exception),
            ['settlement.shift.reason_required'],
        )

        closed = close_clerk_shift(
            access_id=self.admin_access.pk,
            reason='Смена брошена',
        )
        self.assertEqual(closed.status, SettlementClerkShift.Status.CLOSED)
        self.assertEqual(closed.closed_by_access_id, self.admin_access.pk)
        self.assertEqual(closed.close_kind, SettlementClerkShift.CloseKind.ADMIN)
        self.assertEqual(closed.close_reason, 'Смена брошена')

    def test_handover_closes_old_shift_releases_lease_and_opens_new(self):
        open_clerk_shift(access_id=self.clerk_access.pk)
        old_context = self.control_context(self.clerk_access, 'session-one')
        with transaction.atomic():
            lease = lock_settlement_write_lease(
                control_context=old_context,
                using='default',
            )
        self.assertEqual(lease.owner_access_id, self.clerk_access.pk)

        shift, grant = handover_clerk_shift(
            access_id=self.second_clerk_access.pk,
            reason='Передача вахты Петрову',
            raw_session_key='session-two',
        )

        self.assertIsNotNone(grant)
        self.assertEqual(shift.clerk_access_id, self.second_clerk_access.pk)
        self.assertEqual(shift.status, SettlementClerkShift.Status.OPEN)
        previous = SettlementClerkShift.objects.get(
            clerk_access=self.clerk_access,
        )
        self.assertEqual(previous.status, SettlementClerkShift.Status.CLOSED)
        self.assertEqual(previous.close_kind, SettlementClerkShift.CloseKind.HANDOVER)
        self.assertEqual(previous.close_reason, 'Передача вахты Петрову')
        self.assertEqual(
            previous.closed_by_access_id,
            self.second_clerk_access.pk,
        )
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.owner_access_id, self.second_clerk_access.pk)
        released = SettlementControlEvent.objects.filter(
            event_type=SettlementControlEvent.EventType.RELEASED,
        ).order_by('-pk').first()
        self.assertIsNotNone(released)
        self.assertEqual(released.reason, 'Передача вахты: Передача вахты Петрову')

        with self.assertRaises(ValidationError) as captured:
            with transaction.atomic():
                lock_settlement_write_lease(
                    control_context=old_context,
                    using='default',
                )
        self.assertEqual(
            _codes(captured.exception),
            ['settlement.control.busy'],
        )
        with transaction.atomic():
            new_lease = lock_settlement_write_lease(
                control_context=SettlementControlWriteContext(
                    owner_access_id=self.second_clerk_access.pk,
                    raw_session_key='session-two',
                    lease_token=str(grant.lease_token),
                    fencing_revision=grant.fencing_revision,
                ),
                using='default',
            )
        self.assertEqual(new_lease.owner_access_id, self.second_clerk_access.pk)

    def test_handover_does_not_release_parallel_admin_lease(self):
        open_clerk_shift(access_id=self.clerk_access.pk)
        self.control_context(self.admin_access, 'admin-session')

        shift, grant = handover_clerk_shift(
            access_id=self.second_clerk_access.pk,
            reason='Передача при работающем администраторе',
            raw_session_key='second-session',
        )

        self.assertEqual(shift.clerk_access_id, self.second_clerk_access.pk)
        self.assertIsNone(grant)
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.owner_access_id, self.admin_access.pk)
        self.assertFalse(
            SettlementControlEvent.objects.filter(
                event_type=SettlementControlEvent.EventType.RELEASED,
                actor_access_id=self.second_clerk_access.pk,
            ).exists(),
        )
        with transaction.atomic():
            still_admin = lock_settlement_write_lease(
                control_context=SettlementControlWriteContext(
                    owner_access_id=self.admin_access.pk,
                    raw_session_key='admin-session',
                    lease_token=str(lease.lease_token),
                    fencing_revision=lease.fencing_revision,
                ),
                using='default',
            )
        self.assertEqual(still_admin.owner_access_id, self.admin_access.pk)

    def test_handover_without_open_shift_does_not_touch_admin_lease(self):
        self.control_context(self.admin_access, 'admin-session')

        shift, grant = handover_clerk_shift(
            access_id=self.clerk_access.pk,
            reason='Открываю вахту без предшественника',
            raw_session_key='clerk-session',
        )

        self.assertEqual(shift.clerk_access_id, self.clerk_access.pk)
        self.assertIsNone(grant)
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertEqual(lease.owner_access_id, self.admin_access.pk)
        self.assertFalse(
            SettlementControlEvent.objects.filter(
                event_type=SettlementControlEvent.EventType.RELEASED,
            ).exists(),
        )

    def test_service_rejects_inactive_access_and_other_role(self):
        inactive = EmployeeAccess.objects.create(
            employee=self.clerk,
            role=self.clerk_role,
            access_code='SHIFT-INACTIVE',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=False,
        )

        with self.assertRaises(ValidationError) as inactive_error:
            open_clerk_shift(access_id=inactive.pk)
        self.assertEqual(
            _codes(inactive_error.exception),
            ['settlement.shift.inactive_access'],
        )

        with self.assertRaises(ValidationError) as role_error:
            open_clerk_shift(access_id=self.driver_access.pk)
        self.assertEqual(
            _codes(role_error.exception),
            ['settlement.shift.invalid_role'],
        )
        self.assertFalse(SettlementClerkShift.objects.exists())

    def test_state_disables_handover_when_own_previous_watch_shift_open(self):
        SettlementClerkShift.objects.create(
            clerk_access=self.second_clerk_access,
            watch_period=self.old_watch_period,
            status=SettlementClerkShift.Status.OPEN,
            opened_at=timezone.now() - timedelta(days=20),
        )
        open_clerk_shift(access_id=self.clerk_access.pk)

        state = clerk_shift_state(access_id=self.second_clerk_access.pk)

        self.assertFalse(state['can_open'])
        self.assertFalse(state['can_handover'])
        self.assertEqual(
            state['watch_error']['code'],
            'settlement.shift.previous_watch_open',
        )

    def test_second_clerk_acquire_returns_409_shift_taken_over_http(self):
        self.authenticate(self.clerk_access)
        self.post_json('settlement_clerk_shift_open')

        second_client = self.authenticate(self.second_clerk_access, client=Client())
        response = second_client.post(reverse('settlement_control_acquire'))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'settlement.control.shift_taken')

    def test_handover_requires_reason(self):
        open_clerk_shift(access_id=self.clerk_access.pk)

        with self.assertRaises(ValidationError) as captured:
            handover_clerk_shift(
                access_id=self.second_clerk_access.pk,
                reason='   ',
            )

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.shift.reason_required'],
        )
        self.assertTrue(
            SettlementClerkShift.objects.filter(
                status=SettlementClerkShift.Status.OPEN,
                clerk_access=self.clerk_access,
            ).exists(),
        )

    def test_handover_by_current_owner_is_idempotent(self):
        shift = open_clerk_shift(access_id=self.clerk_access.pk)

        same, grant = handover_clerk_shift(
            access_id=self.clerk_access.pk,
            reason='Подтверждаю',
            raw_session_key='session-self',
        )

        self.assertEqual(same.pk, shift.pk)
        self.assertIsNotNone(grant)
        self.assertEqual(
            SettlementClerkShift.objects.filter(
                status=SettlementClerkShift.Status.OPEN,
            ).count(),
            1,
        )


class ClerkShiftLeaseGuardTests(ClerkShiftBase):
    def test_admin_acquires_lease_without_shift(self):
        # Админ пишет без открытой смены клерка — решение владельца 22.11.2026.
        self.assertFalse(
            SettlementClerkShift.objects.filter(
                status=SettlementClerkShift.Status.OPEN,
            ).exists(),
        )
        context = self.control_context(self.admin_access, 'admin-session')
        with transaction.atomic():
            lease = lock_settlement_write_lease(
                control_context=context,
                using='default',
            )
        self.assertEqual(lease.owner_access_id, self.admin_access.pk)

    def test_admin_acquires_lease_despite_foreign_open_shift(self):
        open_clerk_shift(access_id=self.clerk_access.pk)

        context = self.control_context(self.admin_access, 'admin-with-clerk-shift')
        with transaction.atomic():
            lease = lock_settlement_write_lease(
                control_context=context,
                using='default',
            )
        self.assertEqual(lease.owner_access_id, self.admin_access.pk)

    def test_clerk_without_shift_is_rejected_when_watch_is_active(self):
        # Решение владельца 22.11.2026: строгий режим — при активной вахте
        # клерк без своей OPEN-смены писать не может.
        with self.assertRaises(ValidationError) as captured:
            self.control_context(self.clerk_access, 'no-shift-session')

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.control.shift_required'],
        )

    def test_clerk_without_active_watch_keeps_legacy_acquire(self):
        WatchPeriod.objects.filter(pk=self.watch_period.pk).update(is_active=False)

        context = self.control_context(self.clerk_access, 'legacy-session')
        with transaction.atomic():
            lease = lock_settlement_write_lease(
                control_context=context,
                using='default',
            )
        self.assertEqual(lease.owner_access_id, self.clerk_access.pk)

    def test_second_clerk_gets_shift_taken_when_shift_is_open(self):
        open_clerk_shift(access_id=self.clerk_access.pk)

        with self.assertRaises(ValidationError) as captured:
            self.control_context(self.second_clerk_access, 'second-session')

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.control.shift_taken'],
        )

    def test_first_clerk_still_acquires_own_open_shift(self):
        open_clerk_shift(access_id=self.clerk_access.pk)

        context = self.control_context(self.clerk_access, 'own-session')
        with transaction.atomic():
            lease = lock_settlement_write_lease(
                control_context=context,
                using='default',
            )
        self.assertEqual(lease.owner_access_id, self.clerk_access.pk)


class ClerkShiftStateTests(ClerkShiftBase):
    def test_state_without_open_shift_allows_open(self):
        state = clerk_shift_state(access_id=self.clerk_access.pk)

        self.assertIsNotNone(state['watch_period'])
        self.assertIsNone(state['open_shift'])
        self.assertIsNone(state['my_shift'])
        self.assertTrue(state['can_open'])
        self.assertFalse(state['can_close'])
        self.assertFalse(state['can_handover'])

    def test_state_reports_foreign_open_shift_and_handover(self):
        open_clerk_shift(access_id=self.clerk_access.pk)

        state = clerk_shift_state(access_id=self.second_clerk_access.pk)

        self.assertFalse(state['can_open'])
        self.assertTrue(state['can_handover'])
        self.assertFalse(state['open_shift']['is_mine'])
        self.assertEqual(
            state['open_shift']['clerk_access_id'],
            self.clerk_access.pk,
        )

    def test_state_reports_own_shift(self):
        shift = open_clerk_shift(access_id=self.clerk_access.pk)

        state = clerk_shift_state(access_id=self.clerk_access.pk)

        self.assertTrue(state['can_close'])
        self.assertFalse(state['can_handover'])
        self.assertEqual(state['my_shift']['id'], shift.pk)
        self.assertTrue(state['my_shift']['is_mine'])

    def test_state_without_active_watch_reports_error(self):
        WatchPeriod.objects.filter(pk=self.watch_period.pk).update(is_active=False)

        state = clerk_shift_state(access_id=self.clerk_access.pk)

        self.assertIsNone(state['watch_period'])
        self.assertEqual(
            state['watch_error']['code'],
            'settlement.shift.watch_not_active',
        )
        self.assertFalse(state['can_open'])


class ClerkShiftHttpTests(ClerkShiftBase):
    def test_other_role_gets_403_on_shift_endpoints(self):
        self.authenticate(self.driver_access)

        responses = [
            self.client.get(reverse('settlement_clerk_shift_state')),
            self.post_json('settlement_clerk_shift_open'),
            self.post_json('settlement_clerk_shift_close', {'reason': 'нет'}),
            self.post_json('settlement_clerk_shift_handover', {'reason': 'нет'}),
        ]

        for response in responses:
            with self.subTest(url=response.request['PATH_INFO']):
                self.assertEqual(response.status_code, 403)
                self.assert_no_store(response)

    def test_state_open_and_close_over_http(self):
        self.authenticate(self.clerk_access)

        state = self.client.get(reverse('settlement_clerk_shift_state'))
        self.assertEqual(state.status_code, 200)
        self.assert_no_store(state)
        self.assertTrue(state.json()['can_open'])

        opened = self.post_json('settlement_clerk_shift_open')
        self.assertEqual(opened.status_code, 200)
        self.assert_no_store(opened)
        self.assertEqual(opened.json()['shift']['status'], 'open')

        repeated = self.post_json('settlement_clerk_shift_open')
        self.assertEqual(repeated.json()['shift']['id'], opened.json()['shift']['id'])

        closed = self.post_json('settlement_clerk_shift_close', {'reason': ''})
        self.assertEqual(closed.status_code, 200)
        self.assertEqual(closed.json()['shift']['status'], 'closed')

    def test_http_close_ignores_client_close_kind(self):
        self.authenticate(self.clerk_access)
        self.post_json('settlement_clerk_shift_open')

        response = self.post_json(
            'settlement_clerk_shift_close',
            {'reason': 'Подмена вида', 'close_kind': 'admin'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['shift']['close_kind'], 'normal')
        self.assertEqual(
            SettlementClerkShift.objects.get().close_kind,
            SettlementClerkShift.CloseKind.NORMAL,
        )

    def test_second_clerk_open_returns_409_taken(self):
        self.authenticate(self.clerk_access)
        self.post_json('settlement_clerk_shift_open')

        second_client = self.authenticate(self.second_clerk_access, client=Client())
        response = self.post_json('settlement_clerk_shift_open', client=second_client)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'settlement.shift.taken')
        self.assert_no_store(response)

    def test_handover_over_http_issues_lease_to_new_clerk(self):
        self.authenticate(self.clerk_access)
        self.post_json('settlement_clerk_shift_open')

        second_client = self.authenticate(self.second_clerk_access, client=Client())
        response = self.post_json(
            'settlement_clerk_shift_handover',
            {'reason': 'Передача вахты'},
            client=second_client,
        )

        self.assertEqual(response.status_code, 200)
        self.assert_no_store(response)
        payload = response.json()
        self.assertTrue(payload['lease']['acquired'])
        self.assertEqual(
            payload['shift']['clerk_access_id'],
            self.second_clerk_access.pk,
        )
        self.assertEqual(
            SettlementClerkShift.objects.get(
                clerk_access=self.clerk_access,
            ).status,
            SettlementClerkShift.Status.CLOSED,
        )

    def test_handover_without_reason_returns_400(self):
        self.authenticate(self.clerk_access)
        self.post_json('settlement_clerk_shift_open')

        second_client = self.authenticate(self.second_clerk_access, client=Client())
        response = self.post_json(
            'settlement_clerk_shift_handover',
            {},
            client=second_client,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['code'],
            'settlement.shift.reason_required',
        )
        self.assert_no_store(response)
