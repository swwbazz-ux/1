"""Общий вход /start/: что человек видит после ввода номера.

Две вещи, на которых страница врала совмещающему роли: обещала завести пинкод
тому, у кого он уже есть, и вываливала все приложения одним списком.
"""

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Employee, EmployeeAccess, Role


def make_role(code, name):
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': name, 'is_active': True})
    if not role.is_active:
        role.is_active = True
        role.save(update_fields=['is_active'])
    return role


@override_settings(ALLOWED_HOSTS=['testserver', '.testserver'])
class UniversalStartTests(TestCase):
    phone = '+79990000071'

    def setUp(self):
        self.employee = Employee.objects.create(
            full_name='Многоролев Модест Модестович',
            phone=self.phone,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )

    def add_access(self, code, name, *, status=EmployeeAccess.Status.ACTIVATED,
                   access_code='170001', last_login_at=None):
        return EmployeeAccess.objects.create(
            employee=self.employee,
            role=make_role(code, name),
            access_code=access_code,
            status=status,
            is_active=True,
            activated_at=timezone.now(),
            last_login_at=last_login_at,
        )

    def post(self):
        return self.client.post(reverse('universal_start'), {'phone': self.phone})

    def test_person_with_a_working_code_is_not_promised_a_new_one(self):
        self.add_access('driver', 'Водитель самосвала')
        response = self.post()
        self.assertTrue(response.context['has_working_code'])
        self.assertNotContains(response, 'Пинкод придумаете при первом входе')

    def test_person_without_a_code_is_told_to_invent_one(self):
        self.add_access(
            'driver', 'Водитель самосвала',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
            access_code='000000',
        )
        response = self.post()
        self.assertFalse(response.context['has_working_code'])
        self.assertContains(response, 'Пинкод придумаете при первом входе')

    def test_apps_are_shown_as_icon_tiles(self):
        self.add_access('driver', 'Водитель самосвала')
        response = self.post()
        self.assertEqual(len(response.context['apps']), 1)
        self.assertContains(response, 'start-screen__apps')
        self.assertContains(response, response.context['apps'][0]['app'].icon_192_url)

    def test_all_apps_are_shown(self):
        """Раньше список обрезался: кнопки в столбик занимали несколько
        экранов. Плитки в два столбца помещаются, прятать нечего."""
        for code, name in (
            ('driver', 'Водитель самосвала'),
            ('excavator_operator', 'Машинист экскаватора'),
            ('mining_master', 'Горный мастер'),
            ('dispatcher', 'Диспетчер'),
            ('oup', 'Специалист ОУП'),
        ):
            self.add_access(code, name)
        response = self.post()
        self.assertEqual(len(response.context['apps']), 5)

    def test_recently_used_app_comes_first(self):
        now = timezone.now()
        self.add_access('driver', 'Водитель самосвала')
        self.add_access('mining_master', 'Горный мастер', last_login_at=now)
        response = self.post()
        self.assertEqual(response.context['apps'][0]['app'].role_code, 'mining_master')
