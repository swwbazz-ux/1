import json
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from core.production_time import BUSINESS_TIME_ZONE
from reports.dispatcher_duty import duty_report
from reports.dispatcher_duty_period import period_report
from shifts.models import EmployeeShift
from users.active_role import role_session_state
from users.models import Employee, EmployeeAccess, Role

from .dispatcher_duty import SESSION_LOGOUT, session_digest
from .dispatcher_duty_offline import recorder_context
from .models import DispatcherDutyTracking, DispatcherDutyWindow


class OfflineDutyTests(TestCase):
    start = datetime(2026, 11, 25, 19, tzinfo=BUSINESS_TIME_ZONE)

    @classmethod
    def setUpTestData(cls):
        people = []
        for name, code in [('Первый', 'dispatcher'), ('Второй', 'dispatcher'), ('Руководитель', 'manager')]:
            person = Employee.objects.create(full_name=name, status=Employee.Status.ACTIVE)
            role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})
            people.append(EmployeeAccess.objects.create(employee=person, role=role, status=EmployeeAccess.Status.ACTIVATED))
        cls.owner, cls.relief, cls.manager = people
        cls.shift = EmployeeShift.objects.create(employee=cls.owner.employee, workplace_code='dispatcher',
                                                 shift_type='night', opened_at=cls.start)
        DispatcherDutyTracking.objects.update_or_create(pk=1, defaults={'started_at': cls.start})

    def setUp(self):
        self.client = self.login(self.owner)
        self.request = RequestFactory().get('/dispatcher/control/')
        self.request.session = self.client.session
        with patch('django.utils.timezone.now', return_value=self.start):
            self.grant = recorder_context(self.request, role_session_state(self.request))
        self.window = str(uuid4())

    def login(self, access):
        client = Client()
        session = client.session
        session['employee_access_id'] = access.pk
        session.save()
        return client

    def at(self, seconds):
        return self.start + timedelta(seconds=seconds)

    def sample(self, sequence, second, state):
        return [sequence, int(self.at(second).timestamp() * 1000), state]

    def post(self, events, when=90, client=None, **fields):
        data = {'ticket': self.grant['ticket'], 'window': self.window, 'events': json.dumps(events), **fields}
        with patch('django.utils.timezone.now', return_value=self.at(when)):
            return (client or self.client).post(reverse('dispatcher_duty_observations'), data)

    def rows(self):
        return list(DispatcherDutyWindow.objects.filter(shift=self.shift, window_id=self.window)
                    .order_by('started_at', 'pk').values_list('state', 'started_at', 'last_seen_at', 'last_sequence'))

    def test_original_times_on_timeline_and_period_after_retries(self):
        a = self.sample(1, 1, 'visible')
        b = self.sample(2, 15, 'hidden')
        c = self.sample(3, 75, 'hidden')
        d = self.sample(4, 80, 'visible')
        self.assertEqual(self.post([a], when=3).json()['ack'], 1)
        self.assertEqual(self.post([c, d]).json()['ack'], 1)
        self.assertEqual(self.post([a, b, c, d]).json()['ack'], 4)
        self.assertEqual(self.post([b, c], when=123).json()['ack'], 4)
        self.assertEqual(self.rows(), [('visible', self.at(1), self.at(1), 1), ('hidden', self.at(15), self.at(75), 3),
                                      ('visible', self.at(80), self.at(80), 4)])
        day = self.start.date()
        night = duty_report(day, now=self.at(123))['slots'][1]
        self.assertEqual([p['duration'] for p in night['parts'] if 'в фоне' in p['reason']], ['1 мин'])
        self.assertFalse(night['count'])
        self.assertEqual([(m['state'], m['time']) for m in night['window_marks']],
                         [('hidden', '19:00:15'), ('visible', '19:01:20')])
        self.assertEqual([e['window_mark'] for e in night['events'] if e.get('window_mark')],
                         [m['id'] for m in night['window_marks']])
        row, = period_report(day, day, now=self.at(123))['rows']
        self.assertEqual(row['seconds']['background'], 65)
        self.assertEqual(row['count'], 0)
        self.assertEqual([(s.start, s.end) for s, label in row['intervals'] if label == 'Пульт в фоне'],
                         [(self.at(15), self.at(80))])

    def test_restart_after_closed_shift_fences_old_time_and_other_employee(self):
        EmployeeShift.objects.filter(pk=self.shift.pk).update(closed_at=self.at(40))
        again = self.login(self.owner)
        request = RequestFactory().get('/dispatcher/control/')
        request.session = again.session
        self.assertEqual(recorder_context(request, role_session_state(request)), {'owner': str(self.owner.employee_id)})
        samples = [self.sample(1, 5, 'visible'), self.sample(2, 10, 'hidden'), self.sample(3, 55, 'visible')]
        for client in [Client(), self.login(self.relief), self.login(self.manager)]:
            self.assertEqual(self.post(samples, client=client).status_code, 403)
        self.assertEqual(self.rows(), [])
        receipt = self.post(samples, client=again).json()
        self.assertEqual(receipt, {'window': self.window, 'ack': 3, 'retired': True})
        self.post(samples, client=again)
        self.assertEqual([r[0] for r in self.rows()], ['visible', 'hidden'])

    def test_original_session_logout_and_role_generation_fence(self):
        digest = session_digest(self.request)
        DispatcherDutyWindow.objects.create(shift=self.shift, session_digest=digest, window_id=SESSION_LOGOUT,
            state='signed_out', started_at=self.at(28), last_seen_at=self.at(28), last_sequence=0)
        samples = [self.sample(1, 5, 'visible'), self.sample(2, 27, 'hidden'), self.sample(3, 29, 'visible')]
        self.assertTrue(self.post(samples, client=self.login(self.owner)).json()['retired'])
        self.assertEqual([r[0] for r in self.rows()], ['visible', 'hidden'])
        EmployeeAccess.objects.filter(pk=self.owner.pk).update(last_login_at=self.at(14))
        self.window = str(uuid4())
        self.assertTrue(self.post(samples, client=self.login(self.owner)).json()['retired'])
        self.assertEqual([r[0] for r in self.rows()], ['visible'])

    def test_sleep_and_separate_tabs_do_not_fill_an_unobserved_gap(self):
        first = self.window
        self.post([self.sample(1, 5, 'hidden'), self.sample(2, 600, 'hidden')], when=620)
        self.assertEqual(len(self.rows()), 2)
        self.window = str(uuid4())
        self.post([self.sample(1, 6, 'visible'), self.sample(2, 16, 'closed')], when=620)
        self.assertTrue(DispatcherDutyWindow.objects.filter(window_id=first).exists())
        row, = period_report(self.start.date(), self.start.date(), now=self.at(620))['rows']
        self.assertEqual(row['seconds']['visible'], 10)
        self.assertEqual(row['seconds']['unknown'], 450)
        self.assertEqual(row['seconds']['background'], 160)
        marks = duty_report(self.start.date(), now=self.at(620))['slots'][1]['window_marks']
        self.assertEqual([(mark['state'], mark['time']) for mark in marks], [('hidden', '19:00:05')])

    def test_invalid_batch_scope_and_times_do_not_partially_commit(self):
        self.assertEqual(self.post([], ticket='invalid').status_code, 403)
        for reserved in (0, 1):
            self.assertEqual(self.post([self.sample(1, 0, 'visible')], window=str(UUID(int=reserved))).status_code, 400)
        for bad in ([], [self.sample(1, -1, 'visible')], [self.sample(1, 120, 'visible')],
                    [self.sample(1, 1, 'visible'), self.sample(2, 2, 'signed_out')],
                    [self.sample(1, 5, 'visible'), self.sample(2, 3, 'hidden')],
                    [self.sample(1, 0, 'visible'), self.sample(3, 1, 'closed')],
                    [self.sample(n+1, n, 'visible') for n in range(65)]):
            self.assertEqual(self.post(bad).status_code, 400)
        self.assertEqual(self.rows(), [])

    def test_individual_window_changes_remain_visible_when_another_window_is_on_screen(self):
        self.post([self.sample(1, 1, 'visible'), self.sample(2, 20, 'hidden'), self.sample(3, 40, 'visible')])
        self.window = str(uuid4())
        self.post([self.sample(1, 2, 'visible'), self.sample(2, 65, 'visible')])
        night = duty_report(self.start.date(), now=self.at(70))['slots'][1]
        self.assertFalse(any('в фоне' in part['reason'] for part in night['parts']))
        self.assertEqual([mark['time'] for mark in night['window_marks']], ['19:00:20', '19:00:40'])
        self.assertEqual(night['count'], 0)

    def test_clock_adjustment_does_not_block_the_queue_and_a_sleep_gap_uses_actual_time(self):
        future = int(self.at(7200).timestamp() * 1000)
        woke = int(self.at(620).timestamp() * 1000)
        self.post([self.sample(1, 1, 'visible'), [*self.sample(2, 10, 'hidden'), future]], when=20)
        self.assertEqual(self.rows()[-1][1], self.at(10))
        self.post([[*self.sample(3, 15, 'visible'), woke]], when=621)
        self.assertEqual(self.rows()[-1][1], self.at(620))
        row, = period_report(self.start.date(), self.start.date(), now=self.at(621))['rows']
        self.assertGreaterEqual(row['seconds']['unknown'], 460)

    def test_scrolled_archived_day_can_receive_a_bounded_refresh_after_midnight(self):
        selected = self.start.date()
        self.post([self.sample(1, 1, 'visible'), self.sample(2, 20, 'hidden')])
        with patch('django.utils.timezone.now', return_value=self.start + timedelta(days=4)):
            response = self.login(self.manager).get(reverse('management_dispatcher_duty'), {
                'partial': '1', 'before': (selected + timedelta(days=1)).isoformat(), 'since': selected.isoformat(),
            })
        self.assertEqual([d['date'] for d in response.context['report']['days']], [selected])
        self.assertContains(response, 'data-duty-window-mark=')
