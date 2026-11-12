"""Приёмка локального стенда SSE (T-016, WP-1).

Проверяется то, что стенд обязан давать остальным ролям:

- команда создаёт ровно две учётные записи (``driver``, ``excavator_operator``)
  с активированными доступами, открытыми сменами, принятым рабочим назначением
  и принятым путевым назначением самосвала;
- повторный запуск не плодит дубли и не нагоняет версию оперативного состояния
  (иначе каждый перезапуск стенда давал бы лишние SSE-события);
- вход обеих ролей по ``phone`` + ``access_code`` (``users/views.py:1424-1426``)
  даёт 302, а рабочие экраны — 200 с маркерами realtime-клиента;
- ``--reset`` пересоздаёт стенд;
- команда отказывается работать на чужой базе.

Тесты рассчитаны на PostgreSQL-профиль стенда (``config.settings_sse_local``)
и запускаются с ``--keepdb`` на базе ``test_sse_local``.
"""

from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    EquipmentAssignment,
    ExcavatorDumpPointSetting,
    ExcavatorPlacement,
    HaulAssignment,
)
from core.management.commands.setup_sse_stand import (
    Command,
    DRIVER,
    DRIVER_PERSONNEL_NUMBER,
    DUMP_POINT_NAME,
    EXCAVATOR_GARAGE_NUMBER,
    EXCAVATOR_OPERATOR,
    EXCAVATOR_PERSONNEL_NUMBER,
    STAND_PREFIX,
    TRUCK_GARAGE_NUMBER,
)
from core.models import OperationalStateVersion
from references.models import DumpPoint, Equipment
from reports.models import (
    DriverShiftPassportCaptureRequest,
    DriverShiftPassportTrigger,
)
from shifts.models import EmployeeShift
from trips.models import Trip
from users.models import Employee, EmployeeAccess


class SetupSseStandTests(TestCase):
    def setUp(self):
        call_command('setup_sse_stand', verbosity=0)

    def stand_counts(self):
        return {
            'employees': Employee.objects.filter(
                personnel_number__startswith=STAND_PREFIX,
            ).count(),
            'accesses': EmployeeAccess.objects.filter(
                employee__personnel_number__startswith=STAND_PREFIX,
            ).count(),
            'open_shifts': EmployeeShift.objects.filter(
                employee__personnel_number__startswith=STAND_PREFIX,
                closed_at__isnull=True,
            ).count(),
            'work_assignments': EquipmentAssignment.objects.filter(
                employee__personnel_number__startswith=STAND_PREFIX,
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
            ).count(),
            'haul_assignments': HaulAssignment.objects.filter(
                truck__garage_number=TRUCK_GARAGE_NUMBER,
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
            ).count(),
            'trips': Trip.objects.filter(truck__garage_number=TRUCK_GARAGE_NUMBER).count(),
        }

    def test_creates_exactly_two_roles_with_open_shifts_and_assignments(self):
        self.assertEqual(
            Employee.objects.filter(personnel_number__startswith=STAND_PREFIX).count(),
            2,
        )
        self.assertEqual(self.stand_counts(), {
            'employees': 2,
            'accesses': 2,
            'open_shifts': 2,
            'work_assignments': 2,
            'haul_assignments': 1,
            'trips': 1,
        })

        driver = Employee.objects.get(personnel_number=DRIVER_PERSONNEL_NUMBER)
        driver_access = EmployeeAccess.objects.get(employee=driver, role__code='driver')
        self.assertTrue(driver_access.is_active)
        self.assertEqual(driver_access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertEqual(driver_access.access_code, DRIVER['access_code'])
        self.assertEqual(driver.phone, DRIVER['phone'])
        self.assertEqual(driver.status, Employee.Status.ACTIVE)

        driver_shift = EmployeeShift.objects.get(employee=driver, closed_at__isnull=True)
        self.assertEqual(driver_shift.workplace_code, 'driver')
        self.assertEqual(driver_shift.equipment.garage_number, TRUCK_GARAGE_NUMBER)

        excavator_operator = Employee.objects.get(
            personnel_number=EXCAVATOR_PERSONNEL_NUMBER,
        )
        excavator_shift = EmployeeShift.objects.get(
            employee=excavator_operator, closed_at__isnull=True,
        )
        self.assertEqual(excavator_shift.workplace_code, 'excavator_operator')
        self.assertEqual(
            excavator_shift.equipment.garage_number, EXCAVATOR_GARAGE_NUMBER,
        )
        excavator_access = EmployeeAccess.objects.get(
            employee=excavator_operator, role__code='excavator_operator',
        )
        self.assertEqual(
            excavator_access.access_code, EXCAVATOR_OPERATOR['access_code'],
        )

        haul_assignment = HaulAssignment.objects.get(
            truck__garage_number=TRUCK_GARAGE_NUMBER,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
        )
        self.assertEqual(
            haul_assignment.excavator.garage_number, EXCAVATOR_GARAGE_NUMBER,
        )

    def test_second_run_is_idempotent(self):
        before = self.stand_counts()
        version_before = (
            OperationalStateVersion.objects
            .filter(key='production')
            .values_list('version', flat=True)
            .first()
        )

        call_command('setup_sse_stand', verbosity=0)

        self.assertEqual(self.stand_counts(), before)
        version_after = (
            OperationalStateVersion.objects
            .filter(key='production')
            .values_list('version', flat=True)
            .first()
        )
        self.assertEqual(
            version_after, version_before,
            'Повторный запуск не должен порождать события оперативного состояния.',
        )

    def test_reset_recreates_the_stand_without_duplicates(self):
        call_command('setup_sse_stand', '--reset', verbosity=0)
        self.assertEqual(self.stand_counts(), {
            'employees': 2,
            'accesses': 2,
            'open_shifts': 2,
            'work_assignments': 2,
            'haul_assignments': 1,
            'trips': 1,
        })

    def test_driver_login_and_shift_screen(self):
        response = self.client.post('/', {
            'phone': DRIVER['phone'],
            'access_code': DRIVER['access_code'],
        })
        self.assertEqual(response.status_code, 302)

        page = self.client.get('/driver/shift/')
        self.assertEqual(page.status_code, 200)
        html = page.content.decode('utf-8')
        self.assertIn('realtime-client.js', html)
        self.assertIn('data-driver-shell', html)
        self.assertIn('data-driver-shift-open="true"', html)

    def test_excavator_login_and_work_screen(self):
        response = self.client.post('/', {
            'phone': EXCAVATOR_OPERATOR['phone'],
            'access_code': EXCAVATOR_OPERATOR['access_code'],
        })
        self.assertEqual(response.status_code, 302)

        page = self.client.get('/excavator/work/')
        self.assertEqual(page.status_code, 200)
        html = page.content.decode('utf-8')
        self.assertIn('realtime-client.js', html)
        self.assertIn('data-eo-shell', html)
        excavator = EmployeeShift.objects.get(
            employee__personnel_number=EXCAVATOR_PERSONNEL_NUMBER,
            closed_at__isnull=True,
        ).equipment
        self.assertIn(f'data-eo-current-excavator-id="{excavator.pk}"', html)

    def test_reset_removes_stand_rows_that_protect_employee(self):
        """Штатная работа экранов оставляет PROTECT-ссылки; reset их снимает.

        Без правки команда падала на ``Employee`` (QA, T-016-qa-1 §2.1):
        экран экскаваторщика сохраняет ``ExcavatorPlacement.changed_by``, а
        закрытие смены водителем — запрос паспорта смены; оба поля
        ``on_delete=PROTECT``.
        """
        operator = Employee.objects.get(personnel_number=EXCAVATOR_PERSONNEL_NUMBER)
        driver = Employee.objects.get(personnel_number=DRIVER_PERSONNEL_NUMBER)
        excavator = Equipment.objects.get(garage_number=EXCAVATOR_GARAGE_NUMBER)
        dump_point = DumpPoint.objects.get(name=DUMP_POINT_NAME)

        placement = ExcavatorPlacement.objects.create(
            excavator=excavator,
            zone=ExcavatorPlacement.Zone.ACTIVE,
            work_dump_point=dump_point,
            changed_by=operator,
        )
        ExcavatorDumpPointSetting.objects.create(
            placement=placement,
            dump_point=dump_point,
            changed_by=operator,
        )
        driver_shift = EmployeeShift.objects.get(
            employee=driver, closed_at__isnull=True,
        )
        DriverShiftPassportCaptureRequest.objects.create(
            shift=driver_shift,
            request_key='sse-stand-test-passport',
            trigger=DriverShiftPassportTrigger.DRIVER_CLOSE,
            calculator_version='test',
            closed_at=timezone.now(),
            captured_by=operator,
        )

        call_command('setup_sse_stand', '--reset', verbosity=0)

        self.assertEqual(self.stand_counts(), {
            'employees': 2,
            'accesses': 2,
            'open_shifts': 2,
            'work_assignments': 2,
            'haul_assignments': 1,
            'trips': 1,
        })
        self.assertFalse(
            ExcavatorPlacement.objects
            .filter(excavator__garage_number=EXCAVATOR_GARAGE_NUMBER)
            .exists()
        )
        self.assertFalse(DriverShiftPassportCaptureRequest.objects.exists())
        self.assertTrue(EmployeeShift.objects.filter(
            employee__personnel_number=EXCAVATOR_PERSONNEL_NUMBER,
            closed_at__isnull=True,
        ).exists())

    def test_screens_require_login(self):
        self.assertEqual(self.client.get('/driver/shift/').status_code, 302)
        self.assertEqual(self.client.get('/excavator/work/').status_code, 302)

    def test_refuses_foreign_database(self):
        command = Command()
        foreign_settings = mock.Mock()
        foreign_settings.DATABASES = {
            'default': {
                'ENGINE': 'django.db.backends.postgresql',
                'NAME': 'accounting_mvp',
            },
        }
        with mock.patch(
            'core.management.commands.setup_sse_stand.settings', foreign_settings,
        ):
            with self.assertRaisesMessage(CommandError, 'только для локального стенда'):
                command.assert_local_database(False)
