"""T-021: смена производственной специализации отзывает выданный вход.

`sync_employee_production_access` меняет роль существующего `EmployeeAccess`,
деактивирует и реактивирует его. До правки сессии/поколение не трогались:
`reclaim-session` выдавал новую роль без PIN, а деактивация → реактивация
оживляла прежнюю cookie. Здесь проверяются все ветки (R.6 из review).
"""

from datetime import timedelta

from django.contrib.sessions.models import Session
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from shifts.models import WatchPeriod

from .active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
)
from .models import (
    AdminActionLog,
    Employee,
    EmployeeAccess,
    PersonnelPosition,
    ProductionSpecialization,
    Role,
    TemporaryWorkTransfer,
)
from .oup_undo import (
    OUP_ACTION_EMPLOYEE_UPDATED,
    employee_card_undo_state,
    state_change_payload,
    undo_oup_action,
)
from .work_profiles import (
    approve_temporary_work_transfer,
    expire_due_temporary_work_transfers,
    request_temporary_work_transfer,
    sync_employee_production_access,
)


class WorkProfileSessionRevocationTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.driver_role, _created = Role.objects.update_or_create(
            code='driver',
            defaults={'name': 'Водитель самосвала', 'is_active': True},
        )
        self.excavator_role, _created = Role.objects.update_or_create(
            code='excavator_operator',
            defaults={'name': 'Машинист экскаватора', 'is_active': True},
        )
        self.admin_role, _created = Role.objects.update_or_create(
            code='admin',
            defaults={'name': 'Администратор', 'is_active': True},
        )
        self.haul_specialization = ProductionSpecialization.objects.get(
            code='haul_truck_driver',
        )
        self.excavator_specialization = ProductionSpecialization.objects.get(
            code='excavator_operator',
        )
        self.haul_specialization.access_role = self.driver_role
        self.haul_specialization.save(update_fields=['access_role'])
        self.excavator_specialization.access_role = self.excavator_role
        self.excavator_specialization.save(update_fields=['access_role'])
        self.haul_position = PersonnelPosition.objects.get(
            name='Водитель автомобиля, занятый на транспортировании горной массы в технологическом процессе',
        )

        self.employee = Employee.objects.create(
            full_name='Водитель смены профилей T-021',
            status=Employee.Status.ACTIVE,
            is_active=True,
            personnel_position=self.haul_position,
            base_specialization=self.haul_specialization,
            position=self.haul_position.name,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.driver_role,
            access_code='440021',
            status=EmployeeAccess.Status.ACTIVATED,
            activated_at=timezone.now(),
            is_active=True,
        )
        self.client = Client()
        login = self.client.post(
            '/',
            {'access_code': '440021'},
            HTTP_HOST='localhost',
        )
        self.assertEqual(login.status_code, 302)
        self.driver_session_key = self.client.session.session_key
        self.assertIsNotNone(self.driver_session_key)

        self.admin = Employee.objects.create(
            full_name='Администратор T-021',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.admin,
            role=self.admin_role,
            access_code='100030',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    def realtime(self, client):
        return client.get(
            '/realtime/state/',
            {'include_events': '0'},
            HTTP_HOST='localhost',
        )

    def assert_session_is_dead(self, session_key, client=None):
        self.assertFalse(
            Session.objects.filter(session_key=session_key).exists()
        )
        client = client or self.client
        self.assertEqual(self.realtime(client).status_code, 401)
        reclaim = client.post(reverse('reclaim_role_session'), HTTP_HOST='localhost')
        self.assertIn(reclaim.status_code, {401, 403})

    def test_specialization_change_revokes_login_and_reclaim(self):
        self.employee.base_specialization = self.excavator_specialization
        self.employee.save(update_fields=['base_specialization'])

        sync_employee_production_access(employee=self.employee)

        self.access.refresh_from_db()
        self.assertEqual(self.access.role, self.excavator_role)
        self.assertEqual(self.access.access_code, '440021')
        self.assert_session_is_dead(self.driver_session_key)

    def test_deactivation_then_reactivation_does_not_revive_old_cookie(self):
        self.employee.base_specialization = None
        self.employee.save(update_fields=['base_specialization'])
        sync_employee_production_access(employee=self.employee)
        self.access.refresh_from_db()
        self.assertEqual(self.access.status, EmployeeAccess.Status.DEACTIVATED)
        self.assertFalse(self.access.is_active)

        # Сессия, пережившая деактивацию (имитация данных до T-021).
        stale_client = Client()
        session = stale_client.session
        session['employee_access_id'] = self.access.pk
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = self.driver_role.code
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = timezone.now().isoformat()
        session.save()
        stale_session_key = session.session_key
        self.assertTrue(Session.objects.filter(session_key=stale_session_key).exists())

        self.employee.base_specialization = self.haul_specialization
        self.employee.save(update_fields=['base_specialization'])
        sync_employee_production_access(employee=self.employee)

        self.access.refresh_from_db()
        self.assertEqual(self.access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertTrue(self.access.is_active)
        self.assert_session_is_dead(stale_session_key, client=stale_client)

    def test_temporary_transfer_approval_and_expiry_revoke_session(self):
        watch_period = WatchPeriod.objects.create(
            name='Вахта T-021',
            starts_on=self.today,
            ends_on=self.today,
            is_active=True,
        )
        transfer = request_temporary_work_transfer(
            employee=self.employee,
            target_specialization=self.excavator_specialization,
            watch_period=watch_period,
            requested_by=self.admin,
            reason='Перевод T-021',
        )
        approve_temporary_work_transfer(transfer=transfer, reviewed_by=self.admin)

        self.access.refresh_from_db()
        self.assertEqual(self.access.role, self.excavator_role)
        self.assert_session_is_dead(self.driver_session_key)

        # Вход под новой ролью, который должно погасить истечение перевода.
        second_client = Client()
        login = second_client.post('/', {'access_code': '440021'}, HTTP_HOST='localhost')
        self.assertEqual(login.status_code, 302)
        second_session_key = second_client.session.session_key
        self.assertIsNotNone(second_session_key)

        expired = expire_due_temporary_work_transfers(
            as_of=self.today + timedelta(days=1),
        )

        self.assertEqual([item.pk for item in expired], [transfer.pk])
        self.assertEqual(
            TemporaryWorkTransfer.objects.get(pk=transfer.pk).status,
            TemporaryWorkTransfer.Status.EXPIRED,
        )
        self.access.refresh_from_db()
        self.assertEqual(self.access.role, self.driver_role)
        self.assert_session_is_dead(second_session_key, client=second_client)

    def test_oup_undo_of_specialization_change_keeps_new_login_required(self):
        before_state = employee_card_undo_state(self.employee)
        self.employee.base_specialization = self.excavator_specialization
        self.employee.save(update_fields=['base_specialization'])
        sync_employee_production_access(employee=self.employee)
        after_state = employee_card_undo_state(self.employee)

        second_client = Client()
        login = second_client.post('/', {'access_code': '440021'}, HTTP_HOST='localhost')
        self.assertEqual(login.status_code, 302)
        second_session_key = second_client.session.session_key
        self.assertIsNotNone(second_session_key)

        undo_log = AdminActionLog.objects.create(
            actor=self.admin,
            action='ОУП: изменена карточка сотрудника',
            action_code=OUP_ACTION_EMPLOYEE_UPDATED,
            object_type='Employee',
            object_id=str(self.employee.pk),
            undo_payload=state_change_payload(before_state, after_state),
        )

        undo_oup_action(
            log_id=undo_log.pk,
            actor_access_id=self.admin_access.pk,
        )

        self.employee.refresh_from_db()
        self.access.refresh_from_db()
        self.assertEqual(
            self.employee.base_specialization_id,
            self.haul_specialization.pk,
        )
        self.assertEqual(self.access.role, self.driver_role)
        self.assertEqual(self.access.status, EmployeeAccess.Status.ACTIVATED)
        self.assert_session_is_dead(second_session_key, client=second_client)
