from contextlib import redirect_stdout
from datetime import datetime, timedelta
from importlib import import_module
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment, HaulAssignmentHandoff
from core.production_time import BUSINESS_TIME_ZONE
from references.models import Equipment, EquipmentType
from shifts.models import EmployeeShift
from trips.dispatcher_duty import FOREGROUND_HEARTBEAT, SESSION_LOGOUT, WindowState, log_dispatcher_action
from trips.models import DispatcherActionLog, DispatcherActionType, DispatcherDutyTracking, DispatcherDutyWindow
from users.active_role import ACTIVE_ROLE_GENERATION_SESSION_KEY
from users.models import ActiveApplicationSession, Employee, EmployeeAccess, Role

from .dispatcher_duty import FEED_DAYS, duty_feed, duty_report, management_dispatcher_duty


class DispatcherDutyTests(TestCase):
    start = datetime(2026, 11, 25, 7, tzinfo=BUSINESS_TIME_ZONE)

    @classmethod
    def setUpTestData(cls):
        cls.dispatcher = cls.access('dispatcher', 'Диспетчер А')
        cls.relief = cls.access('dispatcher', 'Диспетчер Б')
        cls.manager = cls.access('manager', 'Руководитель')
        cls.admin = cls.access('admin', 'Администратор')
        DispatcherDutyTracking.objects.update_or_create(pk=1, defaults={'started_at': cls.start})

    @staticmethod
    def access(code, name):
        role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})
        employee = Employee.objects.create(full_name=name, status=Employee.Status.ACTIVE)
        return EmployeeAccess.objects.create(employee=employee, role=role, access_code='333333',
                                             status=EmployeeAccess.Status.ACTIVATED)

    def authorize(self, access, client=None):
        client = client or self.client
        session = client.session
        session['employee_access_id'] = access.pk
        session.save()
        return client

    def shift(self, access=None, *, opened=None, closed=None, kind='day', **extra):
        member = (access or self.dispatcher).employee
        return EmployeeShift.objects.create(employee=member, opened_by=member, workplace_code='dispatcher',
                                           shift_type=kind, opened_at=opened or self.start, closed_at=closed, **extra)

    def report(self, elapsed):
        return duty_report(self.start.date(), self.start + elapsed)['slots']

    def test_feed_orders_started_shifts_at_both_handovers_and_midnight(self):
        self.shift(opened=self.start - timedelta(days=2), closed=self.start)
        for delta, expected_date, expected_slots in (
            (timedelta(seconds=-1), self.start.date() - timedelta(days=1), ['night', 'day']),
            (timedelta(), self.start.date(), ['day']),
            (timedelta(hours=12) - timedelta(seconds=1), self.start.date(), ['day']),
            (timedelta(hours=12), self.start.date(), ['night', 'day']),
            (timedelta(hours=17), self.start.date(), ['night', 'day']),
            (timedelta(days=1), self.start.date() + timedelta(days=1), ['day']),
        ):
            with self.subTest(delta=delta):
                days = duty_feed(now=self.start + delta)['days']
                self.assertEqual(days[0]['date'], expected_date)
                self.assertEqual([s['key'] for s in days[0]['slots']], expected_slots)
                slots = [s for day in days for s in day['slots']]
                self.assertTrue(slots[0]['live'])
                self.assertEqual(sum(s['live'] for s in slots), 1)
                self.assertEqual([s['key'] for s in days[1]['slots']], ['night', 'day'])

    def test_feed_reaches_first_record_with_no_missing_or_duplicate_dates(self):
        earliest = self.start - timedelta(days=2 * FEED_DAYS + 1)
        self.shift(opened=earliest, closed=earliest + timedelta(hours=1))
        now = self.start + timedelta(hours=1)
        moments = []
        before = None
        for _ in range(4):
            result = duty_feed(now=now, before=before)
            moments.extend(day['date'] for day in result['days'])
            if not result['next_before']:
                break
            before = result['days'][-1]['date']
            self.assertEqual(result['next_before'], before.isoformat())
        self.assertEqual(moments, [self.start.date() - timedelta(days=i)
                                   for i in range((self.start - earliest).days + 1)])
        self.assertEqual(duty_feed(now=now, since=self.start.date())['days'][0]['date'], moments[0])
        self.assertFalse(duty_feed(now=now, before=earliest.date())['days'])

    def test_dated_bookmark_and_navigation_open_the_feed_and_preserve_dated_json(self):
        self.authorize(self.manager)
        path = reverse('management_dispatcher_duty')
        self.shift(closed=self.start + timedelta(hours=2))
        today = self.start.date() + timedelta(days=1)
        with patch('django.utils.timezone.now', return_value=self.start + timedelta(days=1, hours=2)):
            page = self.client.get(path, {'date': self.start.date()})
            self.assertEqual([d['date'] for d in page.context['report']['days']], [today, self.start.date()])
            self.assertNotContains(page, 'type="date"')
            self.assertContains(page, 'Идёт сейчас', count=1)
            self.assertContains(page, f'href="{path}"')
            history = self.client.get(path, {'before': today, 'partial': '1'})
            self.assertEqual([d['date'] for d in history.context['report']['days']], [self.start.date()])
            self.assertNotContains(history, 'Идёт сейчас')
            fallback = self.client.get(path, {'before': today})
            self.assertEqual(fallback.context['report']['days'][0]['date'], today)
            archive = self.client.get(path, {'format': 'json', 'date': self.start.date()}).json()
            self.assertEqual(archive['date'], self.start.date().isoformat())

    def test_report_only_for_active_management_in_all_representations(self):
        path = reverse('management_dispatcher_duty')
        login = reverse('login', query={'form': '1', 'next': path})
        self.assertRedirects(self.client.get(path), login, fetch_redirect_response=False)
        for role in (self.dispatcher, self.admin, self.access('mining_master', 'Мастер')):
            self.authorize(role)
            for query in ({}, {'partial': 1}, {'format': 'json'}):
                self.assertEqual(self.client.get(path, query).status_code, 403)
        self.authorize(self.manager)
        with override_settings(ALLOWED_HOSTS=['.localhost']):
            self.assertEqual(self.client.get(path, {'format': 'json'}, HTTP_HOST='dispatcher.localhost').status_code, 403)
        url = reverse('management_dashboard')
        self.assertContains(self.client.get(url), path)
        for query in ({}, {'partial': 1}, {'format': 'json'}):
            response = self.client.get(path, query)
            self.assertEqual(response.status_code, 200)
            self.assertIn('no-store', response.headers['Cache-Control'])
        for date in ('junk', '9999-12-31', '0001-01-01'):
            self.assertEqual(self.client.get(path, {'date': date}).status_code, 400)
        request = RequestFactory().get(path)
        request.session = self.client.session
        request.observer_mode = True
        self.assertEqual(management_dispatcher_duty(request).status_code, 403)
        self.manager.last_login_at = timezone.now()
        self.manager.save(update_fields=['last_login_at'])
        session = self.client.session
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = self.start.isoformat()
        session.save()
        self.assertEqual(self.client.get(path, {'format': 'json'}).status_code, 403)

    def test_recorded_windows_timeout_close_logout_and_replayed_beacons(self):
        shift = self.shift()
        self.authorize(self.dispatcher)
        first, second = uuid4(), uuid4()

        def signal(seconds, window, seq, state):
            body = {'path': '/dispatcher/control/', 'duty_shift': shift.pk,
                    'duty_window': str(window), 'duty_sequence': seq, 'duty_state': state}
            route = 'application_session_heartbeat' if state == WindowState.VISIBLE else 'dispatcher_duty_presence'
            with patch('django.utils.timezone.now', return_value=self.start + timedelta(seconds=seconds)):
                response = self.client.post(reverse(route), body)
            self.assertEqual(response.status_code, 204)

        signal(0, first, 1, WindowState.VISIBLE)
        signal(5, second, 1, WindowState.VISIBLE)
        signal(10, first, 2, WindowState.HIDDEN)
        signal(12, first, 1, WindowState.CLOSED)
        signal(30, second, 2, WindowState.CLOSED)
        # Hidden evidence expires at 160; a missing signal must remain a gap after reconnection.
        signal(180, first, 3, WindowState.HIDDEN)
        signal(190, second, 3, WindowState.VISIBLE)
        with patch('django.utils.timezone.now', return_value=self.start + timedelta(seconds=200)):
            self.client.get(reverse('logout'))
        self.assertTrue(shift.duty_windows.filter(window_id=SESSION_LOGOUT).exists())
        day, night = self.report(timedelta(seconds=220))
        totals = {row['state']: row['seconds'] for row in day['totals']}
        self.assertEqual(totals, {'on': 40, 'out': 160, 'unknown': 20, 'future': 43200 - 220})
        self.assertEqual([p['state'] for p in day['parts']], ['on', 'out', 'unknown', 'out', 'on', 'out', 'future'])
        self.assertEqual(night['parts'][0]['state'], 'future')
        self.assertEqual(night['staff'], [])
        self.assertEqual(day['count'], 0)
        self.assertEqual(shift.duty_windows.filter(window_id=first).count(), 3)

    def test_existing_page_foreground_signals_expire_and_yield_to_the_detailed_recorder(self):
        shift = self.shift()
        self.authorize(self.dispatcher)
        heartbeat = reverse('application_session_heartbeat')
        body = {'path': '/dispatcher/control/'}

        def post(at, url=heartbeat, values=body):
            with patch('django.utils.timezone.now', return_value=self.start + timedelta(seconds=at)):
                self.assertEqual(self.client.post(url, values).status_code, 204)

        for seconds in (10, 40, 170, 190):
            post(seconds)
        # A detailed hidden observation arrives after its original time. The
        # fallback must neither overpaint it nor leave a phantom unknown window.
        post(175, reverse('dispatcher_duty_presence'), {'duty_shift': shift.pk, 'duty_window': str(uuid4()),
             'duty_sequence': 1, 'duty_state': 'hidden'})
        post(200)
        day, _ = self.report(timedelta(seconds=210))
        self.assertEqual({t['state']: t['seconds'] for t in day['totals']},
                         {'on': 125, 'out': 35, 'unknown': 50, 'future': 43200 - 210})
        self.assertEqual(list(shift.duty_windows.filter(window_id=FOREGROUND_HEARTBEAT).order_by('pk')
                              .values_list('last_sequence', flat=True)), [2, 4])
        self.assertEqual(day['count'], 0)

    def test_foreground_heartbeat_needs_own_open_duty_and_a_dispatcher_work_page(self):
        self.shift()
        url = reverse('application_session_heartbeat')
        for access, page in ((self.manager, '/reports/'), (self.relief, '/dispatcher/control/'),
                             (self.dispatcher, '/start/')):
            self.authorize(access)
            self.assertEqual(self.client.post(url, {'path': page}).status_code, 204)
        self.assertFalse(DispatcherDutyWindow.objects.exists())

    def test_window_signals_are_scoped_to_own_active_shift(self):
        shift = self.shift()
        body = {'duty_shift': shift.pk, 'duty_window': str(uuid4()), 'duty_sequence': 1, 'duty_state': 'hidden'}
        path = reverse('dispatcher_duty_presence')
        self.authorize(self.manager)
        self.assertEqual(self.client.post(path, body).status_code, 403)
        self.authorize(self.relief)
        self.assertEqual(self.client.post(path, body).status_code, 409)
        self.authorize(self.dispatcher)
        for extra in ({'duty_window': str(SESSION_LOGOUT)}, {'duty_window': str(FOREGROUND_HEARTBEAT)},
                      {'duty_sequence': '-1'}, {'duty_state': 'visible'}):
            self.assertEqual(self.client.post(path, {**body, **extra}).status_code, 400)
        EmployeeShift.objects.filter(pk=shift.pk).update(closed_at=self.start + timedelta(hours=1))
        self.assertEqual(self.client.post(path, body).status_code, 409)
        self.assertFalse(DispatcherDutyWindow.objects.exists())

    def test_shift_handoff_and_midnight_have_real_authors_and_no_duplicate_edges(self):
        evening = self.start + timedelta(hours=12)
        close = evening + timedelta(minutes=4)
        first = self.shift(opened=self.start + timedelta(minutes=8), closed=close,
                           is_service_closed=True, service_close_kind='auto_expired')
        second = self.shift(self.relief, opened=close, closed=evening + timedelta(hours=12), kind='night')
        for at, person, own, target, role in (
            (evening - timedelta(minutes=2), self.dispatcher, first, second, 'dispatcher'),
            (evening + timedelta(hours=5), self.relief, second, first, 'dispatcher'),
            (evening + timedelta(hours=6), self.admin, None, first, 'admin'),
        ):
            row = DispatcherActionLog.objects.create(actor=person.employee, duty_shift=own, shift=target,
                actor_role_code=role, action_type=DispatcherActionType.SERVICE_CLOSE_SHIFT, target_summary='Проверка')
            DispatcherActionLog.objects.filter(pk=row.pk).update(created_at=at)
        day, night = self.report(timedelta(days=1))
        self.assertEqual([(row['start'], row['end']) for row in (day, night)], [('07:00', '19:00'), ('19:00', '07:00')])
        self.assertEqual([row['count'] for row in (day, night)], [1, 1])
        for slot in (day, night):
            self.assertEqual(sum(t['seconds'] for t in slot['totals']), 43200)
        operation, = [e for e in night['events'] if e['kind'] == 'operation']
        self.assertEqual((operation['day'], operation['time'], operation['person']), ('26.11.2026', '00:00', self.relief.employee.full_name))
        auto, = [e for slot in (day, night) for e in slot['events'] if e['title'] == 'Служебное закрытие']
        self.assertEqual((auto['person'], auto['time']), ('Система', '19:04'))
        self.assertEqual(len(night['staff']), 2)
        self.assertEqual([member['operations'] for member in night['staff']], [0, 1])

    def test_coverage_starts_at_installation_and_unknown_archives_do_not_imply_absence(self):
        DispatcherDutyTracking.objects.filter(pk=1).update(started_at=self.start + timedelta(hours=4))
        day, _ = self.report(timedelta(hours=5))
        self.assertEqual([p['state'] for p in day['parts']], ['unknown', 'out', 'future'])
        self.shift(closed=self.start + timedelta(hours=1))
        older = DispatcherActionLog.objects.create(actor=self.dispatcher.employee,
                             action_type=DispatcherActionType.CANCEL_TRIP, target_summary='Рейс № 10')
        DispatcherActionLog.objects.filter(pk=older.pk).update(created_at=self.start + timedelta(minutes=12))
        day, _ = self.report(timedelta(hours=5))
        self.assertEqual(day['count'], 1)
        self.assertFalse(any(p['state'] == 'on' for p in day['parts']))
        EmployeeAccess.objects.create(employee=self.dispatcher.employee, role=self.admin.role, access_code='444444',
                                      status=EmployeeAccess.Status.ACTIVATED)
        self.assertEqual(self.report(timedelta(hours=5))[0]['count'], 0)

    def test_archive_preserves_individual_observations_without_inventing_working_time(self):
        migration = import_module('trips.migrations.0019_archive_dispatcher_shifts_20261125')
        until = self.start + timedelta(hours=24)
        DispatcherDutyTracking.objects.filter(pk=1).update(started_at=until)
        self.shift(closed=self.start + timedelta(hours=12))
        self.shift(self.relief, opened=self.start + timedelta(hours=12), closed=until, kind='night')
        sessions = ActiveApplicationSession.objects
        def snapshot(key, access, minute, *, last_offset=0, path='/dispatcher/control/'):
            at = self.start + timedelta(minutes=minute)
            sessions.create(session_key=key, access=access, role_code=access.role.code, app_code='dispatcher',
                            path=path, foreground_seen_at=at, last_seen_at=at + timedelta(seconds=last_offset))
        snapshot('day', self.dispatcher, 22)
        snapshot('night', self.relief, 1025)
        snapshot('prior-role', self.dispatcher, 26, last_offset=20)
        snapshot('prior-page', self.dispatcher, 29, path='/home/')
        snapshot('off-duty', self.relief, 32)
        snapshot('other-role', self.manager, 22)
        snapshot('outside', self.relief, 1440)
        editor = SimpleNamespace(connection=connection)
        with redirect_stdout(StringIO()) as log:
            migration.archive_shifts(apps, editor)
            migration.archive_shifts(apps, editor)
        self.assertIn('day=available night=available', log.getvalue())
        self.assertEqual(DispatcherDutyWindow.objects.filter(state=WindowState.ARCHIVE_VISIBLE).count(), 2)
        sessions.update(last_seen_at=until, foreground_seen_at=until)
        day, night = self.report(timedelta(days=1))
        for slot, expected in [(day, '07:22'), (night, '00:05')]:
            point, = [e for e in slot['events'] if e['title'] == 'Пульт был на экране']
            self.assertEqual(point['time'], expected)
            self.assertEqual(slot['count'], 0)
            self.assertEqual([(r['state'], r['seconds']) for r in slot['totals']], [('unknown', 43200)])
        migration.remove_archive_points(apps, editor)
        self.assertFalse(DispatcherDutyWindow.objects.exists())

    def test_archive_uses_the_recorded_handoff_for_a_historical_reassignment(self):
        own = self.shift(closed=self.start + timedelta(hours=12))
        DispatcherDutyTracking.objects.filter(pk=1).update(started_at=self.start + timedelta(days=1))
        truck_type = EquipmentType.objects.create(name='Самосвал')
        exc_type = EquipmentType.objects.create(name='Экскаватор')
        truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        old, new = [Equipment.objects.create(equipment_type=exc_type, garage_number=number) for number in ('99', '4')]
        source = HaulAssignment.objects.create(truck=truck, excavator=old, status=AssignmentStatus.ACCEPTED,
                                               ended_at=self.start + timedelta(hours=2))
        target = HaulAssignment.objects.create(truck=truck, excavator=new)
        row = DispatcherActionLog.objects.create(actor=self.dispatcher.employee, haul_assignment=target,
                       action_type=DispatcherActionType.CANCEL_ASSIGNMENT, target_summary='10 назначен под 4')
        DispatcherActionLog.objects.filter(pk=row.pk).update(created_at=self.start + timedelta(hours=2))
        def detail():
            return next(event['detail'] for event in self.report(timedelta(days=1))[0]['events'] if event['kind'] == 'operation')
        self.assertIn('Предыдущий комплекс не записан', detail())
        HaulAssignmentHandoff.objects.create(truck=truck, source_assignment=source, target_assignment=target,
                                             source_excavator=old, source_shift=own)
        self.assertIn('K-99 → K-4', detail())

    def test_real_reassignment_is_attributed_to_the_dispatcher_and_retries_count_once(self):
        own = self.shift(opened=timezone.now() - timedelta(minutes=10))
        self.authorize(self.dispatcher)
        truck = Equipment.objects.create(equipment_type=EquipmentType.objects.create(name='Самосвал'), garage_number='10')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        source, destination = [Equipment.objects.create(equipment_type=excavator_type, garage_number=n) for n in ('99', '4')]
        assigned = HaulAssignment.objects.create(truck=truck, excavator=source, assigned_by=self.dispatcher.employee,
                                                 status=AssignmentStatus.ACCEPTED, accepted_at=timezone.now())
        payload = {'action': 'assign', 'truck_id': truck.pk, 'excavator_id': destination.pk,
                   'expected_assignment_state_id': assigned.pk, 'client_action_id': 'duty-record-once'}
        for _ in range(2):
            self.assertEqual(self.client.post(reverse('dispatcher_assign_truck'), payload, content_type='application/json').status_code, 200)
        row = DispatcherActionLog.objects.get()
        self.assertEqual((row.actor_role_code, row.duty_shift_id), ('dispatcher', own.pk))
        self.assertIn('K-99 → K-4', row.duty_detail)
        self.assertIn('Ожидает подтверждения', row.duty_detail)
        other = self.shift(self.relief, closed=own.opened_at)
        closure = log_dispatcher_action(actor=self.dispatcher.employee, access=self.dispatcher, shift=other,
                          action_type=DispatcherActionType.SERVICE_CLOSE_SHIFT, target_summary='Смена сменщика')
        self.assertEqual((closure.duty_shift_id, closure.shift_id), (own.pk, other.pk))
