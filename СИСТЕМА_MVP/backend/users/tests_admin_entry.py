"""Вход администратора в приложения: свои роли и роли других людей.

Проверяем ровно то, что ломалось руками: пропуск теряется при переходе,
человека нет в списке, потому что он не в смене, и вход в собственную роль
гасит админку.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
    latest_active_role_access,
)
from .live_monitor import OBSERVER_MODE_CONTROL, create_observer_token
from .models import AdminActionLog, Employee, EmployeeAccess, PersonnelPosition, Role


def make_employee(full_name, phone):
    return Employee.objects.create(
        full_name=full_name,
        phone=phone,
        status=Employee.Status.ACTIVE,
        is_active=True,
    )


def make_access(employee, role_code, code, *, logged_in=False):
    role, _ = Role.objects.get_or_create(
        code=role_code,
        defaults={'name': role_code, 'is_active': True},
    )
    if not role.is_active:
        role.is_active = True
        role.save(update_fields=['is_active'])
    return EmployeeAccess.objects.create(
        employee=employee,
        role=role,
        access_code=code,
        status=EmployeeAccess.Status.ACTIVATED,
        is_active=True,
        activated_at=timezone.now(),
        last_login_at=timezone.now() if logged_in else None,
    )


@override_settings(ALLOWED_HOSTS=['testserver', '.testserver'])
class AdminEnterEmployeeTests(TestCase):
    def setUp(self):
        self.admin_employee = make_employee('Админов Админ Админович', '+79990000001')
        self.admin_access = make_access(self.admin_employee, 'admin', '100001', logged_in=True)

        # Водитель без смены и без сессии: в «Смене онлайн» его не видно.
        self.idle_employee = make_employee('Простоев Пётр Петрович', '+79990000002')
        self.idle_access = make_access(self.idle_employee, 'driver', '100002')

        session = self.client.session
        session['employee_access_id'] = self.admin_access.pk
        session[ACTIVE_ROLE_SESSION_KEY] = self.admin_access.pk
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = self.admin_access.last_login_at.isoformat()
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = 'admin'
        session.save()

    def test_lists_employee_who_is_not_on_shift(self):
        response = self.client.get(reverse('system_admin_enter_employee'))
        self.assertEqual(response.status_code, 200)
        names = [row['employee'].full_name for row in response.context['rows']]
        self.assertIn('Простоев Пётр Петрович', names)

    def test_search_narrows_the_list(self):
        response = self.client.get(reverse('system_admin_enter_employee'), {'q': 'Простоев'})
        names = {row['employee'].full_name for row in response.context['rows']}
        self.assertEqual(names, {'Простоев Пётр Петрович'})

    def test_own_roles_are_offered_separately(self):
        make_access(self.admin_employee, 'mining_master', '100001')
        response = self.client.get(reverse('system_admin_enter_employee'))
        own_roles = {item['access'].role.code for item in response.context['own_apps']}
        self.assertIn('mining_master', own_roles)
        # Сама админка в список входа не попадает: в ней он уже находится.
        self.assertNotIn('admin', own_roles)

    def test_entering_own_role_keeps_admin_session_active(self):
        """Активная роль у сотрудника одна, и админка гаснуть не должна."""
        own = make_access(self.admin_employee, 'mining_master', '100001')
        token = create_observer_token(
            actor_access=self.admin_access,
            target_access=own,
            mode=OBSERVER_MODE_CONTROL,
        )
        self.client.get(
            '/mining-master/assignments/',
            {'observe': token},
            HTTP_HOST='mining-master.testserver',
        )
        active = latest_active_role_access(self.admin_employee)
        self.assertEqual(active.pk, self.admin_access.pk)


@override_settings(ALLOWED_HOSTS=['testserver', '.testserver'])
class ObserverTokenSurvivalTests(TestCase):
    def setUp(self):
        self.admin_employee = make_employee('Админов Админ Админович', '+79990000011')
        self.admin_access = make_access(self.admin_employee, 'admin', '110001', logged_in=True)
        self.driver_employee = make_employee('Водителев Иван Иванович', '+79990000012')
        self.driver_access = make_access(self.driver_employee, 'driver', '110002', logged_in=True)

    def control_token(self):
        return create_observer_token(
            actor_access=self.admin_access,
            target_access=self.driver_access,
            mode=OBSERVER_MODE_CONTROL,
        )

    def test_redirect_keeps_the_token(self):
        token = self.control_token()
        response = self.client.get(
            '/driver/',
            {'observe': token},
            HTTP_HOST='driver.testserver',
        )
        if 300 <= response.status_code < 400:
            self.assertIn('observe=', response['Location'])
        else:
            self.assertEqual(response.status_code, 200)

    def test_watch_mode_still_blocks_changes(self):
        watch = create_observer_token(
            actor_access=self.admin_access,
            target_access=self.driver_access,
        )
        response = self.client.post(
            f'/driver/?observe={watch}',
            HTTP_HOST='driver.testserver',
        )
        self.assertEqual(response.status_code, 403)


class GrantAllRoleAccessCommandTests(TestCase):
    def setUp(self):
        self.employee = make_employee('Админов Админ Админович', '+79990000021')
        self.admin_access = make_access(self.employee, 'admin', '120001', logged_in=True)
        for code in ('driver', 'mining_master', 'dispatcher'):
            Role.objects.get_or_create(code=code, defaults={'name': code, 'is_active': True})

    def test_creates_accesses_with_the_same_code(self):
        call_command('grant_all_role_access', phone='+79990000021', apply=True, verbosity=0, stdout=StringIO())
        codes = set(
            EmployeeAccess.objects
            .filter(employee=self.employee)
            .values_list('role__code', flat=True)
        )
        self.assertIn('mining_master', codes)
        self.assertIn('dispatcher', codes)
        for access in EmployeeAccess.objects.filter(employee=self.employee):
            self.assertEqual(access.access_code, '120001')
            self.assertEqual(access.status, EmployeeAccess.Status.ACTIVATED)

    def test_admin_stays_the_active_role(self):
        call_command('grant_all_role_access', phone='+79990000021', apply=True, verbosity=0, stdout=StringIO())
        active = latest_active_role_access(self.employee)
        self.assertEqual(active.pk, self.admin_access.pk)

    def test_dry_run_changes_nothing(self):
        before = EmployeeAccess.objects.filter(employee=self.employee).count()
        call_command('grant_all_role_access', phone='+79990000021', verbosity=0, stdout=StringIO())
        self.assertEqual(EmployeeAccess.objects.filter(employee=self.employee).count(), before)

    def test_repeated_run_is_safe(self):
        call_command('grant_all_role_access', phone='+79990000021', apply=True, verbosity=0, stdout=StringIO())
        first = EmployeeAccess.objects.filter(employee=self.employee).count()
        call_command('grant_all_role_access', phone='+79990000021', apply=True, verbosity=0, stdout=StringIO())
        self.assertEqual(EmployeeAccess.objects.filter(employee=self.employee).count(), first)


@override_settings(ALLOWED_HOSTS=['testserver', '.testserver'])
class EmployeePositionFilterTests(TestCase):
    """Фильтр по кадровой должности был в разметке, но данные в него не шли:
    список открывался пустым, и выбор в нём ничего не менял."""

    def setUp(self):
        self.admin_employee = make_employee('Админов Админ Админович', '+79990000061')
        self.admin_access = make_access(self.admin_employee, 'admin', '160001', logged_in=True)
        # Часть должностей заводит миграция — берём заведомо своё название.
        self.position, _ = PersonnelPosition.objects.get_or_create(
            name='Проверочная должность для фильтра',
            defaults={'is_active': True},
        )
        self.with_position = make_employee('Сдолжностев Семён', '+79990000062')
        self.with_position.personnel_position = self.position
        self.with_position.save(update_fields=['personnel_position', 'updated_at'])
        self.without_position = make_employee('Бездолжностев Борис', '+79990000063')

        session = self.client.session
        session['employee_access_id'] = self.admin_access.pk
        session[ACTIVE_ROLE_SESSION_KEY] = self.admin_access.pk
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = self.admin_access.last_login_at.isoformat()
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = 'admin'
        session.save()

    def names(self, response):
        return {employee.full_name for employee in response.context['employees']}

    def test_positions_reach_the_page(self):
        response = self.client.get(reverse('system_admin_employees'))
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.position, list(response.context['personnel_positions']))
        self.assertTrue(response.context['personnel_position_groups'])

    def test_filter_by_position_narrows_the_list(self):
        response = self.client.get(
            reverse('system_admin_employees'), {'personnel_position': str(self.position.pk)},
        )
        self.assertEqual(self.names(response), {'Сдолжностев Семён'})

    def test_filter_finds_employees_without_a_position(self):
        response = self.client.get(
            reverse('system_admin_employees'), {'personnel_position': 'none'},
        )
        names = self.names(response)
        self.assertIn('Бездолжностев Борис', names)
        self.assertNotIn('Сдолжностев Семён', names)

    def test_no_filter_shows_everyone(self):
        response = self.client.get(reverse('system_admin_employees'))
        names = self.names(response)
        self.assertIn('Бездолжностев Борис', names)
        self.assertIn('Сдолжностев Семён', names)
