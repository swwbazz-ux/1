"""Страница «Как идёт тест»: считаем полевых людей, а не свои роли.

У владельца системы доступы во все приложения, и почти во всех он ни разу не
был. Пока его карточку считали наравне со всеми, страница показывала одиннадцать
человек, которые «ни разу не вошли», — и за ними пришлось бы гоняться.
"""

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
)
from .models import Employee, EmployeeAccess, Role
from .protected_cards import allow_protected_card_write


def make_role(code, name):
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': name, 'is_active': True})
    return role


@override_settings(ALLOWED_HOSTS=['testserver', '.testserver'])
class FieldTestCountsTests(TestCase):
    def setUp(self):
        self.owner = Employee.objects.create(
            full_name='Владельцев Владелец Владелецович',
            phone='+79990000081',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.owner,
            role=make_role('admin', 'Администратор'),
            access_code='180001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            activated_at=timezone.now(),
            last_login_at=timezone.now(),
        )
        # Роли, в которые владелец ни разу не заходил.
        for code, name in (
            ('driver', 'Водитель самосвала'),
            ('excavator_operator', 'Машинист экскаватора'),
            ('mining_master', 'Горный мастер'),
        ):
            EmployeeAccess.objects.create(
                employee=self.owner,
                role=make_role(code, name),
                access_code='180001',
                status=EmployeeAccess.Status.ACTIVATED,
                is_active=True,
                activated_at=timezone.now(),
            )

        # Настоящий полевой водитель, который тоже ещё не входил.
        self.field_worker = Employee.objects.create(
            full_name='Полевой Пётр Петрович',
            phone='+79990000082',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=self.field_worker,
            role=make_role('driver', 'Водитель самосвала'),
            access_code='180002',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            activated_at=timezone.now(),
        )

        session = self.client.session
        session['employee_access_id'] = self.admin_access.pk
        session[ACTIVE_ROLE_SESSION_KEY] = self.admin_access.pk
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = self.admin_access.last_login_at.isoformat()
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = 'admin'
        session.save()

    def totals(self):
        response = self.client.get(reverse('system_admin_field_test'))
        self.assertEqual(response.status_code, 200)
        return response.context['totals']

    def test_unprotected_owner_still_inflates_the_numbers(self):
        """Опорная точка: без защиты владелец считается наравне со всеми."""
        self.assertEqual(self.totals()['never_logged_in'], 4)

    def test_protected_card_is_left_out_of_people_counts(self):
        with allow_protected_card_write():
            self.owner.is_protected = True
            self.owner.save(update_fields=['is_protected', 'updated_at'])
        totals = self.totals()
        # Остаётся только настоящий полевой водитель.
        self.assertEqual(totals['never_logged_in'], 1)
        self.assertEqual(totals['activated'], 1)
        self.assertEqual(totals['accesses_total'], 1)

    def test_by_role_table_loses_the_phantoms(self):
        with allow_protected_card_write():
            self.owner.is_protected = True
            self.owner.save(update_fields=['is_protected', 'updated_at'])
        response = self.client.get(reverse('system_admin_field_test'))
        rows = {row['role__code']: row['total'] for row in response.context['by_role']}
        self.assertEqual(rows.get('driver'), 1)
        self.assertNotIn('admin', rows)
        self.assertNotIn('mining_master', rows)
