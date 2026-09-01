from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from .live_monitor import presence_by_employee_id
from .models import ActiveApplicationSession, Employee, EmployeeAccess, Role


class EmployeePresenceTests(TestCase):
    def setUp(self):
        self.role = Role.objects.create(code='driver', name='Водитель самосвала')

    def employee(self, suffix):
        return Employee.objects.create(
            full_name=f'Сотрудник {suffix}',
            phone=f'+79990000{suffix:03d}',
        )

    def activated_access(self, employee):
        now = timezone.now()
        return EmployeeAccess.objects.create(
            employee=employee,
            role=self.role,
            access_code='1234',
            status=EmployeeAccess.Status.ACTIVATED,
            activated_at=now,
            last_login_at=now,
        )

    def test_distinguishes_not_registered_offline_recent_and_online(self):
        not_registered = self.employee(1)
        offline = self.employee(2)
        recent = self.employee(3)
        online = self.employee(4)
        offline_access = self.activated_access(offline)
        recent_access = self.activated_access(recent)
        online_access = self.activated_access(online)
        now = timezone.now()
        ActiveApplicationSession.objects.create(
            session_key='recent-session',
            access=recent_access,
            role_code='driver',
            app_code='driver',
            path='/driver/',
            last_seen_at=now - timedelta(minutes=5),
        )
        ActiveApplicationSession.objects.create(
            session_key='online-session',
            access=online_access,
            role_code='driver',
            app_code='driver',
            path='/driver/',
            last_seen_at=now,
        )

        payload = presence_by_employee_id(
            [not_registered.id, offline.id, recent.id, online.id],
            now=now,
        )

        self.assertEqual(payload[not_registered.id]['status'], 'not_registered')
        self.assertEqual(payload[offline.id]['status'], 'offline')
        self.assertEqual(payload[recent.id]['status'], 'recent')
        self.assertEqual(payload[online.id]['status'], 'online')
        self.assertEqual(payload[online.id]['app_code'], 'driver')
