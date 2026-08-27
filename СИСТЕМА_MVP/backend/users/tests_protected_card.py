"""Защищённая карточка владельца системы.

Смысл проверок один: карточку нельзя изменить обычными путями системы. Если
однажды её перепишет загрузка из отдела кадров или закроет чужая рука, войти и
всё починить будет уже неоткуда — поэтому запрет должен держать и там, где о
нём никто не помнил.
"""

from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .models import Employee, EmployeeAccess, Role
from .protected_cards import allow_protected_card_write


class ProtectedCardWriteTests(TestCase):
    def setUp(self):
        self.employee = Employee.objects.create(
            full_name='Владельцев Владелец Владелецович',
            phone='+79990000031',
            position='Водитель самосвала',
            status=Employee.Status.ACTIVE,
            is_active=True,
            is_protected=True,
        )
        self.role, _ = Role.objects.get_or_create(
            code='admin', defaults={'name': 'Администратор', 'is_active': True},
        )

    def test_plain_save_is_refused(self):
        self.employee.position = 'Кто угодно'
        with self.assertRaises(ValidationError):
            self.employee.save()

    def test_reloaded_object_cannot_be_saved_either(self):
        fresh = Employee.objects.get(pk=self.employee.pk)
        fresh.full_name = 'Подменов Подмен'
        with self.assertRaises(ValidationError):
            fresh.save()

    def test_protection_cannot_be_switched_off_by_assignment(self):
        """Иначе снять защиту можно было бы одной строчкой присваивания."""
        fresh = Employee.objects.get(pk=self.employee.pk)
        fresh.is_protected = False
        with self.assertRaises(ValidationError):
            fresh.save()
        self.assertTrue(Employee.objects.get(pk=self.employee.pk).is_protected)

    def test_bulk_update_is_refused(self):
        with self.assertRaises(ValidationError):
            Employee.objects.filter(pk=self.employee.pk).update(is_active=False)

    def test_delete_is_refused(self):
        with self.assertRaises(ValidationError):
            self.employee.delete()
        self.assertTrue(Employee.objects.filter(pk=self.employee.pk).exists())

    def test_access_cannot_be_created_or_removed(self):
        """Доступ — часть карточки: сняв его, владельца запрут снаружи."""
        with self.assertRaises(ValidationError):
            EmployeeAccess.objects.create(
                employee=self.employee,
                role=self.role,
                access_code='131313',
                status=EmployeeAccess.Status.ACTIVATED,
                is_active=True,
            )

    def test_existing_access_cannot_be_deactivated(self):
        with allow_protected_card_write():
            access = EmployeeAccess.objects.create(
                employee=self.employee,
                role=self.role,
                access_code='131313',
                status=EmployeeAccess.Status.ACTIVATED,
                is_active=True,
                activated_at=timezone.now(),
            )
        access.is_active = False
        with self.assertRaises(ValidationError):
            access.save()
        with self.assertRaises(ValidationError):
            access.delete()

    def test_login_stamp_still_works(self):
        """Вход отмечается в доступе. Запретив это, мы заперли бы владельца."""
        with allow_protected_card_write():
            access = EmployeeAccess.objects.create(
                employee=self.employee,
                role=self.role,
                access_code='131313',
                status=EmployeeAccess.Status.ACTIVATED,
                is_active=True,
                activated_at=timezone.now(),
            )
        moment = timezone.now()
        access.last_login_at = moment
        access.save(update_fields=['last_login_at'])
        self.assertEqual(
            EmployeeAccess.objects.get(pk=access.pk).last_login_at, moment,
        )

    def test_login_stamp_cannot_smuggle_other_fields(self):
        with allow_protected_card_write():
            access = EmployeeAccess.objects.create(
                employee=self.employee,
                role=self.role,
                access_code='131313',
                status=EmployeeAccess.Status.ACTIVATED,
                is_active=True,
            )
        access.last_login_at = timezone.now()
        access.is_active = False
        with self.assertRaises(ValidationError):
            access.save(update_fields=['last_login_at', 'is_active'])

    def test_owner_path_still_works(self):
        with allow_protected_card_write():
            self.employee.position = 'Системный администратор'
            self.employee.save(update_fields=['position', 'updated_at'])
        self.assertEqual(
            Employee.objects.get(pk=self.employee.pk).position,
            'Системный администратор',
        )

    def test_unprotected_employees_are_untouched(self):
        other = Employee.objects.create(
            full_name='Обычнов Обычен Обычнович',
            phone='+79990000032',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        other.position = 'Водитель самосвала'
        other.save()
        Employee.objects.filter(pk=other.pk).update(is_active=True)
        self.assertEqual(Employee.objects.get(pk=other.pk).position, 'Водитель самосвала')


class ProtectedCardCommandTests(TestCase):
    def setUp(self):
        self.employee = Employee.objects.create(
            full_name='Владельцев Владелец Владелецович',
            phone='+79990000041',
            position='Водитель самосвала',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        driver, _ = Role.objects.get_or_create(
            code='driver', defaults={'name': 'Водитель самосвала', 'is_active': True},
        )
        Role.objects.get_or_create(code='admin', defaults={'name': 'Администратор', 'is_active': True})
        EmployeeAccess.objects.create(
            employee=self.employee,
            role=driver,
            access_code='140001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            activated_at=timezone.now(),
            last_login_at=timezone.now(),
        )

    def run_command(self, **kwargs):
        call_command(
            'grant_all_role_access',
            phone='+79990000041',
            verbosity=0,
            stdout=StringIO(),
            **kwargs,
        )

    def test_position_and_protection_are_applied(self):
        self.run_command(position='Системный администратор', protect=True, apply=True)
        fresh = Employee.objects.get(pk=self.employee.pk)
        self.assertEqual(fresh.position, 'Системный администратор')
        self.assertTrue(fresh.is_protected)
        self.assertTrue(
            EmployeeAccess.objects
            .filter(employee=fresh, role__code='admin', status=EmployeeAccess.Status.ACTIVATED)
            .exists()
        )

    def test_command_still_works_on_an_already_protected_card(self):
        """Иначе владелец защитил бы карточку и потерял способ её чинить."""
        self.run_command(protect=True, apply=True)
        self.run_command(position='Системный администратор', apply=True)
        self.assertEqual(
            Employee.objects.get(pk=self.employee.pk).position,
            'Системный администратор',
        )

    def test_dry_run_does_not_protect(self):
        self.run_command(position='Системный администратор', protect=True)
        fresh = Employee.objects.get(pk=self.employee.pk)
        self.assertFalse(fresh.is_protected)
        self.assertEqual(fresh.position, 'Водитель самосвала')


class ProtectedCardSurvivesImportTests(TestCase):
    def test_bulk_import_skips_the_protected_card(self):
        """Главная угроза: загрузка списка сотрудников переписывает карточки."""
        from .management.commands.import_oup_employees import Command

        employee = Employee.objects.create(
            full_name='Владельцев Владелец Владелецович',
            phone='+79990000051',
            position='Системный администратор',
            status=Employee.Status.ACTIVE,
            is_active=True,
            is_protected=True,
        )
        command = Command()
        item = {'full_name': employee.full_name, 'personnel_number': ''}
        command._find_employee = lambda _item: (employee, '')
        outcome, employee_id, reason = command._import_item(
            item, commit=True, source_label='тест',
        )
        self.assertEqual(outcome, 'skipped')
        self.assertEqual(reason, 'карточка защищена от изменений')
        self.assertEqual(
            Employee.objects.get(pk=employee.pk).position,
            'Системный администратор',
        )
