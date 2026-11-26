from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook

from core.production_time import BUSINESS_TIME_ZONE
from shifts.models import EmployeeShift
from trips.models import DispatcherActionLog, DispatcherDutyTracking, DispatcherDutyWindow
from users.models import Employee, EmployeeAccess, Role

from .dispatcher_duty_period import period_report


class PeriodReportTests(TestCase):
    start = datetime(2026, 11, 25, 7, tzinfo=BUSINESS_TIME_ZONE)

    @classmethod
    def setUpTestData(cls):
        cls.manager, cls.dispatcher, cls.relief = [cls.access(role, person) for role, person in
            [('manager', 'Руководитель'), ('dispatcher', '=Диспетчер А'), ('dispatcher', 'Диспетчер Б')]]
        DispatcherDutyTracking.objects.update_or_create(pk=1, defaults={'started_at': cls.start, 'history_from': None})

    @staticmethod
    def access(code, name):
        role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})
        employee = Employee.objects.create(full_name=name, status=Employee.Status.ACTIVE)
        return EmployeeAccess.objects.create(employee=employee, role=role, access_code='111111',
                                            status=EmployeeAccess.Status.ACTIVATED)

    def at(self, minutes):
        return self.start + timedelta(minutes=minutes)

    def shift(self, opened, closed=None, *, employee=None, kind='day'):
        member = employee or self.dispatcher.employee
        return EmployeeShift.objects.create(employee=member, workplace_code='dispatcher', shift_type=kind,
            opened_by=member, opened_at=self.at(opened), closed_at=self.at(closed) if closed is not None else None)

    def window(self, shift, window, state, first, last):
        DispatcherDutyWindow.objects.create(shift=shift, session_digest='a' * 64, window_id=window,
            state=state, started_at=self.at(first), last_seen_at=self.at(last), last_sequence=1)

    def command(self, shift, minute, *, actor=None, role='dispatcher'):
        event = DispatcherActionLog.objects.create(actor=actor or self.dispatcher.employee, duty_shift=shift,
            actor_role_code=role, action_type='cancel_trip', target_summary='Рейс № 21')
        DispatcherActionLog.objects.filter(pk=event.pk).update(created_at=self.at(minute))

    def test_multiple_windows_background_quiet_spells_and_real_commands(self):
        shift = self.shift(0, 420)
        window, other = uuid4(), uuid4()
        for state, first, last in [('visible',0,9), ('hidden',10,309), ('visible',310,369), ('closed',370,370)]:
            self.window(shift, window, state, first, last)
        self.window(shift, other, 'visible',90,104)
        self.window(shift, other, 'closed',105,105)
        for minute in (4,97,330):
            self.command(shift, minute)
        self.command(shift, 6, actor=self.manager.employee, role='manager')
        report = period_report(self.start.date(), self.start.date(), self.at(430))
        row, = report['rows']
        self.assertEqual(dict(row['seconds']), {'visible':85*60, 'background':285*60, 'away':50*60})
        self.assertEqual(row['background_percent'],67.9)
        self.assertEqual((row['first_op'], row['last_op'], row['count']), (self.at(4), self.at(330), 3))
        self.assertEqual((row['longest_background'].start, row['longest_background'].end), (self.at(105), self.at(310)))
        self.assertEqual((row['visible_gap'].start, row['visible_gap'].end), (self.at(330), self.at(370)))
        self.assertEqual(report['gap_seconds'], 10 * 60)
        self.assertEqual(report['people'][0]['duration'], 420 * 60)

    def test_missing_heartbeats_and_old_point_never_become_background_hours(self):
        shift = self.shift(0)
        self.window(shift, uuid4(), 'archive_visible',0,0)
        self.window(shift, uuid4(), 'hidden',2,2)
        report = period_report(self.start.date(), self.start.date(), self.at(30))
        row, = report['rows']
        self.assertEqual(row['seconds']['background'], 150)
        self.assertEqual(row['seconds']['unknown'], 30*60 - 150)
        self.assertEqual(row['background_percent'],8.3)
        self.assertIsNone(row['visible_gap'])
        self.assertIsNone(row['closed'])
        self.assertEqual(report['gaps'], [])
        self.assertEqual(report['people'][0]['duration'], 30*60)

    def test_range_clips_overnight_carry_in_and_excludes_next_seven(self):
        before = self.shift(-30, 15, kind='night')
        night = self.shift(721, 1470, kind='night')
        for minute in (-1,0,14):
            self.command(before, minute)
        for minute in (900,1439,1440):
            self.command(night, minute)
        self.shift(1440, 1500, employee=self.relief.employee)
        report = period_report(self.start.date(), self.start.date(), self.at(1500))
        latest, carried = report['rows']
        self.assertEqual((latest['count'],carried['count']), (2,2))
        self.assertEqual((latest['span'].end,latest['closed']), (self.at(1440),self.at(1470)))
        self.assertEqual((carried['opened'],carried['span'].start), (self.at(-30),self.at(0)))
        self.assertEqual(report['people'][0]['seconds']['unknown'], (15+1440-721)*60)
        self.assertIsNone(report['people'][0]['background_percent'])
        self.assertEqual(report['gap_seconds'],(721-15)*60)

    def test_own_hours_are_a_union_and_only_confirmed_own_commands_are_reported(self):
        DispatcherDutyTracking.objects.filter(pk=1).update(started_at=self.at(50))
        first = self.shift(0,60)
        second = self.shift(30,90)
        self.shift(75,115,employee=self.relief.employee)
        self.command(None,40,role='')
        self.command(None,10,role='')
        self.command(first,20,actor=self.relief.employee)
        self.command(second,95)
        self.command(None,120)
        self.command(first,15,role='admin')
        self.command(first,16,role='observer')
        report = period_report(self.start.date(),self.start.date(),self.at(125))
        own = next(p for p in report['people'] if p['person']==self.dispatcher.employee.full_name)
        self.assertEqual((own['duties'],own['duration'],own['count']), (2,90*60,1))
        self.assertEqual(report['gap_seconds'],10*60)
        self.assertEqual([e['when'] for row in report['rows'] for e in row['commands']], [self.at(10)])

        self.login_as(self.manager)
        path = reverse('management_dispatcher_period')
        query = {'date_from': self.start.date(), 'date_to': self.start.date()}
        with patch('django.utils.timezone.now', return_value=self.at(125)):
            page = self.client.get(path, query)
            export = self.client.get(path, {**query, 'export': 'xlsx'})
        self.assertNotContains(page, 'Операции без привязки к личной смене')
        self.assertContains(page, 'Журнал операций (1)', count=1)
        workbook = load_workbook(BytesIO(export.content), read_only=True)
        self.assertEqual(workbook['Операции'].max_row, 3)
        commands = list(workbook['Операции'].values)
        self.assertEqual(commands[2][:3], (self.dispatcher.employee.full_name,
                                           self.start.replace(tzinfo=None), self.at(10).replace(tzinfo=None)))
        workbook.close()

    def test_future_is_empty_and_pretracking_vacancy_unknown(self):
        DispatcherDutyTracking.objects.filter(pk=1).update(started_at=self.at(100))
        report = period_report(self.start.date(),self.start.date(),self.at(55))
        self.assertEqual(report['gaps'], [])
        self.assertFalse(report['covers_post'])
        tomorrow=self.at(1440).date()
        future=period_report(tomorrow,tomorrow,self.at(55))
        self.assertTrue(future['future'])
        self.assertFalse(future['rows'])
        self.assertFalse(future['covers_post'])

    def login_as(self, access):
        session = self.client.session
        session['employee_access_id'] = access.pk
        session.save()

    def test_html_xlsx_share_access_checks_and_input_validation(self):
        path = reverse('management_dispatcher_period')
        for query in ({},{'export':'xlsx'}):
            self.assertEqual(self.client.get(path,query).status_code,302)
        self.login_as(self.dispatcher)
        for query in ({},{'export':'xlsx'}):
            self.assertEqual(self.client.get(path,query).status_code,403)
        self.login_as(self.manager)
        with override_settings(ALLOWED_HOSTS=['.localhost']):
            self.assertEqual(self.client.get(path,{'export':'xlsx'},HTTP_HOST='dispatcher.localhost').status_code,403)
        self.assertEqual(self.client.get(path,{'date_from':'2026-12-01','date_to':'2026-11-25'}).status_code,400)
        self.assertEqual(self.client.get(path,{'date_to':'2026-11-25'}).status_code,400)
        self.assertEqual(self.client.get(path,{'date_from':'oops','date_to':'9999-12-31','export':'xlsx'}).status_code,400)
        with patch('django.utils.timezone.now',return_value=self.at(10)):
            page = self.client.get(path)
        self.assertEqual(page.context['report']['date_from'],self.start.date()-timedelta(days=6))
        self.assertIn('no-store',page.headers['Cache-Control'])

    def test_xlsx_contains_all_pages_typed_durations_and_plain_text_names(self):
        for i in range(28):
            shift = self.shift(i*10,i*10+8)
            self.command(shift,i*10+2)
        self.login_as(self.manager)
        path = reverse('management_dispatcher_period')
        query = {'date_from':'2026-11-25','date_to':'2026-11-25'}
        with patch('django.utils.timezone.now',return_value=self.at(400)):
            page=self.client.get(path,{**query,'page':'2'})
            result=self.client.get(path,{**query,'page':'2','export':'xlsx'})
        self.assertEqual(len(page.context['page'].object_list),3)
        self.assertEqual(result.status_code,200)
        self.assertIn('private',result.headers['Cache-Control'])
        self.assertIn('2026-11-25', result.headers['Content-Disposition'])
        book=load_workbook(BytesIO(result.content))
        self.assertEqual(book['Личные смены'].max_row,30)
        self.assertEqual(book['Операции'].max_row,30)
        self.assertEqual(book['По диспетчерам']['A3'].data_type,'s')
        self.assertEqual(book['По диспетчерам']['C3'].value,timedelta(minutes=224))
        self.assertEqual(book['Личные смены']['C3'].value,self.at(270).replace(tzinfo=None))
