from datetime import datetime, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from references.models import DumpPoint, Equipment, EquipmentModel, EquipmentType, RockType
from shifts.models import EmployeeShift
from users.models import Employee, EmployeeAccess, Role

from .excavator_hourly_report import build_excavator_hourly_report
from .models import Trip, TripStatus


class ExcavatorHourlyReportTests(TestCase):
    def setUp(self):
        self.excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck_type = EquipmentType.objects.create(name='Самосвал')
        self.excavator_model = EquipmentModel.objects.create(
            equipment_type=self.excavator_type,
            name='ЭКГ тест',
        )
        self.belaz_model = EquipmentModel.objects.create(
            equipment_type=self.truck_type,
            name='БелАЗ 7513',
        )
        self.nhl_model = EquipmentModel.objects.create(
            equipment_type=self.truck_type,
            name='NHL NTE240',
        )
        self.excavator = Equipment.objects.create(
            equipment_type=self.excavator_type,
            model=self.excavator_model,
            garage_number='ЭКС-99',
        )
        self.other_excavator = Equipment.objects.create(
            equipment_type=self.excavator_type,
            model=self.excavator_model,
            garage_number='ЭКС-100',
        )
        self.belaz = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.belaz_model,
            garage_number='58',
        )
        self.nhl = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.nhl_model,
            garage_number='N-1',
        )
        self.unknown = Equipment.objects.create(
            equipment_type=self.truck_type,
            garage_number='U-1',
        )
        self.point_a = DumpPoint.objects.create(name='Склад 2.1')
        self.point_b = DumpPoint.objects.create(name='ККД')
        self.actual_point = DumpPoint.objects.create(name='СКДР')
        self.rock = RockType.objects.create(name='Руда тест', density='2.5', loosening_factor='1.4')
        self.role = Role.objects.create(code='excavator_operator', name='Машинист экскаватора')
        self.operator = Employee.objects.create(full_name='Иванов И.И.', status=Employee.Status.ACTIVE)
        self.access = EmployeeAccess.objects.create(
            employee=self.operator,
            role=self.role,
            access_code='778899',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )

    def trip(self, truck, loaded_at, *, excavator=None, status=TripStatus.COMPLETED,
             dump_point=None, assigned_dump_point=None, actual_dump_point=None,
             loading_shift=None, passive=False, legacy_without_assignment=False):
        dump_point = dump_point or self.point_a
        return Trip.objects.create(
            excavator=excavator or self.excavator,
            truck=truck,
            excavator_operator=self.operator,
            loading_shift=loading_shift,
            rock_type=self.rock,
            dump_point=dump_point,
            assigned_dump_point=(
                None if legacy_without_assignment
                else (assigned_dump_point or dump_point)
            ),
            actual_dump_point=actual_dump_point,
            status=status,
            loaded_at=loaded_at,
            driver_participation_recorded=passive,
            driver_control_shift=None if passive else loading_shift,
        )

    def test_counts_canonical_load_time_point_and_fleet_without_shift_cutoff(self):
        captured_at = timezone.make_aware(datetime(2026, 9, 14, 11, 24))
        old_shift = EmployeeShift.objects.create(
            employee=self.operator,
            equipment=self.excavator,
            shift_type='day',
            opened_at=captured_at - timedelta(hours=4),
            closed_at=captured_at - timedelta(minutes=50),
        )
        self.trip(self.belaz, captured_at - timedelta(minutes=75), loading_shift=old_shift)
        self.trip(
            self.nhl,
            captured_at - timedelta(minutes=15),
            assigned_dump_point=self.point_b,
            actual_dump_point=self.actual_point,
        )
        self.trip(self.belaz, captured_at - timedelta(minutes=5), passive=True)
        self.trip(self.unknown, captured_at - timedelta(minutes=2), dump_point=self.point_b)
        self.trip(
            self.belaz,
            captured_at - timedelta(minutes=1),
            actual_dump_point=self.actual_point,
            legacy_without_assignment=True,
        )
        self.trip(self.belaz, captured_at - timedelta(minutes=3), status=TripStatus.CANCELLED)
        self.trip(self.belaz, captured_at - timedelta(minutes=4), excavator=self.other_excavator)

        with self.assertNumQueries(1):
            report = build_excavator_hourly_report(self.excavator, captured_at=captured_at)

        by_point = {group['dump_point']: group for group in report['groups']}
        self.assertEqual(by_point['Склад 2.1']['rows'][0], {
            'code': 'belaz', 'label': 'БелАЗ', 'previous': 1, 'current': 1,
        })
        self.assertNotIn('СКДР', by_point)
        self.assertEqual(by_point['ККД']['rows'][0]['code'], 'nhl')
        self.assertEqual(by_point['Точка не определена']['rows'][0]['code'], 'belaz')
        self.assertEqual(by_point['ККД']['rows'][1]['code'], 'unknown')
        self.assertEqual(report['totals']['grand'], {'previous': 1, 'current': 4})
        self.assertFalse(report['is_empty'])

    def test_empty_report_keeps_grand_total_and_omits_empty_groups(self):
        captured_at = timezone.make_aware(datetime(2026, 9, 14, 11, 24))
        report = build_excavator_hourly_report(self.excavator, captured_at=captured_at)

        self.assertEqual(report['groups'], [])
        self.assertEqual(report['totals']['rows'], [])
        self.assertEqual(report['totals']['grand'], {'previous': 0, 'current': 0})
        self.assertTrue(report['is_empty'])

    def test_midnight_period_labels_include_both_dates(self):
        captured_at = timezone.make_aware(datetime(2026, 9, 14, 0, 17))
        report = build_excavator_hourly_report(self.excavator, captured_at=captured_at)

        self.assertEqual(report['periods']['previous']['label'], '13.09 23:00–14.09 00:00')
        self.assertEqual(report['periods']['current']['label'], '00:00–00:17')

    def test_endpoint_uses_only_excavator_from_open_shift(self):
        now = timezone.now()
        EmployeeShift.objects.create(
            employee=self.operator,
            equipment=self.excavator,
            workplace_code='excavator_operator',
            shift_type='day',
            opened_at=now - timedelta(hours=2),
        )
        self.trip(self.belaz, now - timedelta(minutes=10))
        self.trip(self.nhl, now - timedelta(minutes=9), excavator=self.other_excavator)
        session = self.client.session
        session['employee_access_id'] = self.access.pk
        session.save()

        response = self.client.get(reverse('excavator_hourly_report'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(response.json()['excavator']['id'], self.excavator.pk)
        self.assertEqual(response.json()['totals']['grand']['current'], 1)

    def test_endpoint_requires_open_shift_and_work_screen_exposes_entry(self):
        session = self.client.session
        session['employee_access_id'] = self.access.pk
        session.save()
        report_response = self.client.get(reverse('excavator_hourly_report'))
        self.assertEqual(report_response.status_code, 409)
        self.assertEqual(report_response.json()['code'], 'open_shift_required')

        work_response = self.client.get(reverse('excavator_work'))
        self.assertContains(work_response, 'data-eo-hourly-report-open')
        self.assertContains(work_response, reverse('excavator_hourly_report'))
        self.assertContains(work_response, 'excavator-mobile-shell-v236')
