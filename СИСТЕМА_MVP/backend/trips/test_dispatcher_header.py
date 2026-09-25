"""Контракты общей шапки Диспетчерского пульта."""

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from shifts.models import ShiftType
from users.models import Employee, EmployeeAccess, Role

from .dispatcher_header import build_dispatcher_header_context


class DispatcherHeaderContextTests(TestCase):
    def setUp(self):
        role = Role.objects.create(code='dispatcher', name='Горный диспетчер')
        employee = Employee.objects.create(
            full_name='Диспетчер шапки',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code='442211',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    def test_production_shift_date_and_calendar_clock_date_are_separate(self):
        local_datetime = timezone.make_aware(datetime(2026, 9, 26, 5, 54))
        production_context = SimpleNamespace(
            local_datetime=local_datetime,
            production_date=date(2026, 9, 25),
            shift_type=ShiftType.NIGHT,
        )

        with patch(
            'trips.dispatcher_header.production_shift_context',
            return_value=production_context,
        ):
            context = build_dispatcher_header_context(self.access)

        self.assertEqual(context['current_date'], '25.09.2026')
        self.assertEqual(context['clock_date'], '26.09.2026')
        self.assertEqual(context['shift_label'], 'Вторая смена')
