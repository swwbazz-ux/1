from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from references.models import Equipment, EquipmentModel, EquipmentType
from shifts.models import EmployeeShift

from .active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
)
from .live_monitor import create_observer_token
from .models import (
    ActiveApplicationSession,
    AdminActionLog,
    Employee,
    EmployeeAccess,
    Role,
)
from .role_apps import ROLE_APPS


class AdminLiveMonitorTests(TestCase):
    def setUp(self):
        cache.clear()
        now = timezone.now()
        self.admin_role = Role.objects.create(code='admin', name='Администратор')
        self.dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.driver_role = Role.objects.create(code='driver', name='Водитель')
        self.admin = Employee.objects.create(
            full_name='Тест Администратор',
            status=Employee.Status.ACTIVE,
        )
        self.dispatcher = Employee.objects.create(
            full_name='Тест Диспетчер',
            status=Employee.Status.ACTIVE,
        )
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.admin,
            role=self.admin_role,
            access_code='910001',
            status=EmployeeAccess.Status.ACTIVATED,
            last_login_at=now,
        )
        self.dispatcher_access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=self.dispatcher_role,
            access_code='910002',
            status=EmployeeAccess.Status.ACTIVATED,
            last_login_at=now,
        )
        self._activate_client(self.client, self.admin_access)

    @staticmethod
    def _activate_client(client, access):
        session = client.session
        session['employee_access_id'] = access.pk
        session[ACTIVE_ROLE_SESSION_KEY] = access.pk
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = access.role.code
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = access.last_login_at.isoformat()
        session.save()
        return session.session_key

    @staticmethod
    def _standalone_session(access):
        session = SessionStore()
        session['employee_access_id'] = access.pk
        session[ACTIVE_ROLE_SESSION_KEY] = access.pk
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = access.role.code
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = access.last_login_at.isoformat()
        session.save()
        return session.session_key

    def test_admin_screen_lists_every_ready_application_and_is_private(self):
        response = self.client.get(reverse('system_admin_live_monitor'), HTTP_HOST='admin.localhost')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertContains(response, 'Смена онлайн')
        self.assertContains(response, f'<strong>{len(ROLE_APPS)}</strong>', html=True)
        for app in ROLE_APPS:
            self.assertContains(response, f'data-app-code="{app.role_code}"')

    def test_non_admin_cannot_open_monitor_or_run_actions(self):
        client = Client()
        self._activate_client(client, self.dispatcher_access)

        page = client.get(reverse('system_admin_live_monitor'), HTTP_HOST='dispatcher.localhost')
        action = client.post(
            reverse('system_admin_force_end_sessions', args=[self.admin_access.pk]),
            {'reason': 'Недопустимая попытка'},
            HTTP_HOST='dispatcher.localhost',
        )

        self.assertEqual(page.status_code, 302)
        self.assertEqual(action.status_code, 302)
        self.assertTrue(self.admin_access.is_active)

    def test_read_only_page_does_not_write_heartbeat_and_explicit_post_records_path(self):
        client = Client()
        session_key = self._activate_client(client, self.dispatcher_access)

        page = client.get('/dispatcher/control/', HTTP_HOST='dispatcher.localhost')
        self.assertEqual(page.status_code, 200)
        self.assertFalse(ActiveApplicationSession.objects.filter(session_key=session_key).exists())

        posted = client.post(
            reverse('application_session_heartbeat'),
            {'path': '/dispatcher/control/'},
            HTTP_HOST='dispatcher.localhost',
        )
        self.assertEqual(posted.status_code, 204)
        heartbeat = ActiveApplicationSession.objects.get(session_key=session_key)
        self.assertEqual(heartbeat.app_code, 'dispatcher')
        self.assertEqual(heartbeat.path, '/dispatcher/control/')

        cache.clear()
        previous_seen_at = heartbeat.last_seen_at
        poll = client.get(reverse('operational_state_version'), HTTP_HOST='dispatcher.localhost')
        self.assertEqual(poll.status_code, 200)
        heartbeat.refresh_from_db()
        self.assertEqual(heartbeat.path, '/dispatcher/control/')
        self.assertEqual(heartbeat.last_seen_at, previous_seen_at)

    def test_observer_token_opens_real_role_screen_read_only(self):
        token = create_observer_token(
            actor_access=self.admin_access,
            target_access=self.dispatcher_access,
        )

        response = Client().get(
            '/dispatcher/control/',
            {'observe': token},
            HTTP_HOST='dispatcher.localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')
        self.assertContains(response, 'РЕЖИМ НАБЛЮДЕНИЯ')
        self.assertContains(response, self.dispatcher.full_name)
        self.assertContains(response, 'observer-mode.js')

    def test_observer_realtime_uses_target_role_but_cannot_post(self):
        token = create_observer_token(
            actor_access=self.admin_access,
            target_access=self.dispatcher_access,
        )
        client = Client()

        realtime = client.get(
            reverse('operational_state_version'),
            HTTP_X_OBSERVER_TOKEN=token,
            HTTP_HOST='dispatcher.localhost',
        )
        blocked = client.post(
            '/dispatcher/shift/toggle/',
            {'action': 'open'},
            HTTP_X_OBSERVER_TOKEN=token,
            HTTP_HOST='dispatcher.localhost',
        )

        self.assertEqual(realtime.status_code, 200)
        self.assertTrue(realtime.json()['authenticated'])
        self.assertTrue(realtime.json()['role_active'])
        self.assertEqual(blocked.status_code, 403)
        self.assertFalse(EmployeeShift.objects.filter(employee=self.dispatcher).exists())
        self.assertFalse(ActiveApplicationSession.objects.filter(access=self.dispatcher_access).exists())

    def test_observer_token_is_bound_to_target_application(self):
        token = create_observer_token(
            actor_access=self.admin_access,
            target_access=self.dispatcher_access,
        )

        response = Client().get('/driver/', {'observe': token}, HTTP_HOST='driver.localhost')

        self.assertEqual(response.status_code, 403)

    def test_force_end_removes_all_target_sessions_and_keeps_admin_session(self):
        target_keys = {
            self._standalone_session(self.dispatcher_access),
            self._standalone_session(self.dispatcher_access),
        }
        for session_key in target_keys:
            ActiveApplicationSession.objects.create(
                session_key=session_key,
                access=self.dispatcher_access,
                role_code='dispatcher',
                app_code='dispatcher',
                path='/dispatcher/control/',
                last_seen_at=timezone.now(),
            )
        old_revision = self.dispatcher_access.last_login_at
        admin_session_key = self.client.session.session_key

        response = self.client.post(
            reverse('system_admin_force_end_sessions', args=[self.dispatcher_access.pk]),
            {'reason': 'Проверка служебного отключения'},
            HTTP_HOST='admin.localhost',
        )

        self.assertRedirects(response, reverse('system_admin_live_monitor'))
        from django.contrib.sessions.models import Session
        self.assertFalse(Session.objects.filter(session_key__in=target_keys).exists())
        self.assertTrue(Session.objects.filter(session_key=admin_session_key).exists())
        self.assertFalse(ActiveApplicationSession.objects.filter(access=self.dispatcher_access).exists())
        self.dispatcher_access.refresh_from_db()
        self.assertGreater(self.dispatcher_access.last_login_at, old_revision)
        self.assertTrue(AdminActionLog.objects.filter(
            action_code='admin_sessions_force_end',
            comment='Проверка служебного отключения',
        ).exists())

    def test_force_close_non_equipment_shift_is_audited(self):
        shift = EmployeeShift.objects.create(
            employee=self.dispatcher,
            shift_type='day',
            workplace_code='dispatcher',
            opened_at=timezone.now() - timedelta(hours=3),
            opened_by=self.dispatcher,
        )

        response = self.client.post(
            reverse('system_admin_force_close_shift', args=[shift.pk]),
            {'reason': 'Сотрудник покинул рабочее место'},
            HTTP_HOST='admin.localhost',
        )

        self.assertRedirects(response, reverse('system_admin_live_monitor'))
        shift.refresh_from_db()
        self.assertIsNotNone(shift.closed_at)
        self.assertEqual(shift.closed_by, self.admin)
        self.assertTrue(shift.is_service_closed)
        self.assertTrue(AdminActionLog.objects.filter(
            action_code='admin_shift_force_close',
            object_id=str(shift.pk),
            comment='Сотрудник покинул рабочее место',
        ).exists())

    def test_force_close_truck_requires_and_saves_real_end_readings(self):
        driver = Employee.objects.create(
            full_name='Тест Водитель',
            status=Employee.Status.ACTIVE,
        )
        EmployeeAccess.objects.create(
            employee=driver,
            role=self.driver_role,
            access_code='910003',
            status=EmployeeAccess.Status.ACTIVATED,
            last_login_at=timezone.now(),
        )
        equipment_type = EquipmentType.objects.create(name='Самосвал карьерный')
        model = EquipmentModel.objects.create(
            equipment_type=equipment_type,
            name='БелАЗ QA',
            fuel_capacity_limit_l=2000,
        )
        truck = Equipment.objects.create(
            equipment_type=equipment_type,
            model=model,
            garage_number='LIVE-QA-01',
        )
        shift = EmployeeShift.objects.create(
            employee=driver,
            shift_type='night',
            workplace_code='driver',
            equipment=truck,
            start_fuel=Decimal('1000'),
            start_mileage=Decimal('20000'),
            start_engine_hours=Decimal('5000'),
            opened_at=timezone.now() - timedelta(hours=4),
            opened_by=driver,
        )

        rejected = self.client.post(
            reverse('system_admin_force_close_shift', args=[shift.pk]),
            {'reason': 'Нет фактических показаний'},
            HTTP_HOST='admin.localhost',
        )
        shift.refresh_from_db()
        self.assertIsNone(shift.closed_at)
        self.assertRedirects(rejected, reverse('system_admin_live_monitor'))

        accepted = self.client.post(
            reverse('system_admin_force_close_shift', args=[shift.pk]),
            {
                'reason': 'Водитель передал технику сменщику',
                'end_fuel': '925,5',
                'end_mileage': '20080',
                'end_engine_hours': '5008',
            },
            HTTP_HOST='admin.localhost',
        )
        self.assertRedirects(accepted, reverse('system_admin_live_monitor'))
        shift.refresh_from_db()
        self.assertEqual(shift.end_fuel, Decimal('925.50'))
        self.assertEqual(shift.end_mileage, Decimal('20080.00'))
        self.assertEqual(shift.end_engine_hours, Decimal('5008.00'))
        self.assertTrue(shift.is_service_closed)

    def test_empty_reason_never_changes_shift_or_sessions(self):
        shift = EmployeeShift.objects.create(
            employee=self.dispatcher,
            shift_type='day',
            workplace_code='dispatcher',
            opened_at=timezone.now(),
        )
        session_key = self._standalone_session(self.dispatcher_access)

        close = self.client.post(
            reverse('system_admin_force_close_shift', args=[shift.pk]),
            {'reason': '   '},
            HTTP_HOST='admin.localhost',
        )
        eject = self.client.post(
            reverse('system_admin_force_end_sessions', args=[self.dispatcher_access.pk]),
            {'reason': ''},
            HTTP_HOST='admin.localhost',
        )

        self.assertEqual(close.status_code, 302)
        self.assertEqual(eject.status_code, 302)
        shift.refresh_from_db()
        self.assertIsNone(shift.closed_at)
        from django.contrib.sessions.models import Session
        self.assertTrue(Session.objects.filter(session_key=session_key).exists())

    def test_observer_javascript_blocks_writes_and_login_exit_links(self):
        script = (settings.BASE_DIR / 'static' / 'js' / 'observer-mode.js').read_text(
            encoding='utf-8'
        )

        self.assertIn('X-Observer-Token', script)
        self.assertIn('["GET", "HEAD", "OPTIONS"]', script)
        self.assertIn('event.preventDefault()', script)
        self.assertIn('control.disabled = true', script)
        self.assertIn('"/logout/"', script)
        self.assertIn('link.removeAttribute("href")', script)

    def test_heartbeat_javascript_uses_protected_post_not_read_only_get(self):
        script = (
            settings.BASE_DIR / 'static' / 'js' / 'application-session-heartbeat.js'
        ).read_text(encoding='utf-8')

        self.assertIn('method: "POST"', script)
        self.assertIn('"X-CSRFToken": csrfToken', script)
        self.assertIn('body.set("path", window.location.pathname)', script)
        self.assertIn('intervalMs = 30000', script)
