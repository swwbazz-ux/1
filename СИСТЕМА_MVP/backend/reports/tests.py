from datetime import datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse
from django.test import RequestFactory, TestCase
from django.utils import timezone

from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import DumpPoint, Equipment, EquipmentModel, EquipmentType, RockType, TruckCapacityRule
from shifts.models import EmployeeShift, EquipmentPlanGroup, ShiftPlan
from trips.models import Trip, TripClientAction, TripStatus
from users.models import Employee, EmployeeAccess, Role

from .shift_analytics import build_excavator_dynamics, build_shift_analytics
from .views import (
    build_mechanic_downtime_rows,
    dispatcher_downtime_filters,
    dispatcher_downtime_queryset,
    dispatcher_mining_filters,
    dispatcher_mining_trip_queryset,
    dispatcher_reports_context,
    dispatcher_shift_downtime_rows,
    dispatcher_shift_log_filters,
    dispatcher_shift_trip_rows,
    downtime_daily_summary,
    management_dashboard_context,
    management_dynamics_report_context,
    shift_analytics_report_context,
)


class ManagementDashboardPlanTests(TestCase):
    def test_management_dashboard_uses_manual_shift_plan_without_trips(self):
        role = Role.objects.create(code='manager', name='Руководство')
        employee = Employee.objects.create(full_name='Руководитель')
        access = EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code='6000',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        ShiftPlan.objects.create(
            date='2026-07-04',
            shift_type='day',
            name='План дневной смены',
            plan_volume_m3='2500.00',
            is_active=True,
        )
        request = RequestFactory().get('/reports/management/?date=2026-07-04')

        context = management_dashboard_context(request, access)

        self.assertEqual(context['daily_plan_total'], Decimal('2500.00'))
        self.assertEqual(context['daily_plan_source'], 'из сменных планов админки')
        self.assertEqual(context['daily_total_volume'], 0)
        self.assertEqual(context['daily_plan_completion_percent'], Decimal('0.0'))


class ProductionConsumerContractTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.control_instant = datetime(2026, 7, 23, 15, 30, tzinfo=dt_timezone.utc)
        self.production_date = self.control_instant.astimezone(
            timezone.get_fixed_timezone(10 * 60)
        ).date() - timedelta(days=1)
        self.excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck_type = EquipmentType.objects.create(name='Самосвал')
        self.excavator = Equipment.objects.create(
            equipment_type=self.excavator_type,
            garage_number='Э-КОНТРАКТ',
        )
        self.truck = Equipment.objects.create(
            equipment_type=self.truck_type,
            garage_number='С-КОНТРАКТ',
        )
        self.rock = RockType.objects.create(name='Руда контракта')
        self.dump_point = DumpPoint.objects.create(name='ККД контракта')
        self.reason = DowntimeReason.objects.create(
            name='Простой контракта',
            equipment_type=self.excavator_type,
        )

    def test_control_instant_uses_previous_production_date_in_all_default_consumers_and_plans(self):
        request = self.factory.get('/reports/')
        instant_representations = (
            self.control_instant,
            self.control_instant.astimezone(timezone.get_fixed_timezone(4 * 60)),
            self.control_instant.astimezone(timezone.get_fixed_timezone(10 * 60)),
        )
        for instant in instant_representations:
            with self.subTest(instant=instant), patch(
                'core.production_time.timezone.now',
                return_value=instant,
            ), patch('django.utils.timezone.now', return_value=instant):
                self.assertEqual(dispatcher_mining_filters(request)['date'], self.production_date)
                self.assertEqual(dispatcher_downtime_filters(request)['date'], self.production_date)
                self.assertEqual(dispatcher_shift_log_filters(request)['date'], self.production_date)
                self.assertEqual(
                    shift_analytics_report_context(request)['date_value'],
                    self.production_date.isoformat(),
                )
                dynamics = management_dynamics_report_context(request)
                self.assertEqual(dynamics['date_to_value'], self.production_date.isoformat())

        with patch('core.production_time.timezone.now', return_value=self.control_instant), patch(
            'django.utils.timezone.now',
            return_value=self.control_instant,
        ):
            group = EquipmentPlanGroup.objects.create(
                code='production-contract',
                name='Плановая группа контракта',
                plan_value='10.00',
            )
            plan = ShiftPlan.objects.create(name='Сменный план контракта')

        self.assertEqual(group.active_from, self.production_date)
        self.assertEqual(plan.date, self.production_date)

    def test_control_instant_is_in_previous_production_date_reports(self):
        Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            volume_m3='31.00',
            completed_at=self.control_instant,
        )
        DowntimeEvent.objects.create(
            equipment=self.excavator,
            reason=self.reason,
            started_at=self.control_instant,
        )
        request = self.factory.get('/reports/', {'date': self.production_date.isoformat()})

        mining_filters = dispatcher_mining_filters(request)
        downtime_filters = dispatcher_downtime_filters(request)
        log_filters = dispatcher_shift_log_filters(request)

        self.assertEqual(dispatcher_mining_trip_queryset(mining_filters).count(), 1)
        self.assertEqual(dispatcher_downtime_queryset(downtime_filters).count(), 1)
        self.assertEqual(len(dispatcher_shift_downtime_rows(log_filters)), 1)
        self.assertEqual(len(dispatcher_shift_trip_rows(log_filters)), 1)

    def test_downtime_daily_summary_uses_production_date_after_midnight(self):
        rows = [{
            'started_at': self.control_instant,
            'ended_at': self.control_instant + timedelta(hours=1),
            'is_critical': False,
            'duration_hours': Decimal('1.00'),
        }]

        summary = downtime_daily_summary(rows)

        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]['date'], self.production_date)


class ReportSemanticsRegressionTests(TestCase):
    def setUp(self):
        self.role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.employee = Employee.objects.create(full_name='Диспетчер отчетов')
        self.access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.role,
            access_code='500100',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        self.excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck_type = EquipmentType.objects.create(name='Самосвал')
        self.excavator = Equipment.objects.create(
            equipment_type=self.excavator_type,
            garage_number='Э-1',
        )
        self.truck = Equipment.objects.create(
            equipment_type=self.truck_type,
            garage_number='10',
        )
        self.rock = RockType.objects.create(name='Руда')
        self.dump_point = DumpPoint.objects.create(name='ККД')
        self.selected_date = timezone.localdate()
        self.event_at = timezone.make_aware(
            datetime.combine(self.selected_date, time(10, 0)),
            timezone.get_current_timezone(),
        )

    def test_mechanic_block_excludes_refueling_and_includes_mechanical_reason(self):
        mechanical, _ = DowntimeReason.objects.update_or_create(
            name='Ремонт',
            defaults={'equipment_type': self.excavator_type, 'show_for_mechanic': True},
        )
        refueling, _ = DowntimeReason.objects.update_or_create(
            name='Заправка',
            defaults={'equipment_type': self.excavator_type, 'show_for_mechanic': False},
        )
        DowntimeEvent.objects.create(
            equipment=self.excavator,
            reason=mechanical,
            started_at=self.event_at,
        )
        DowntimeEvent.objects.create(
            equipment=self.excavator,
            reason=refueling,
            started_at=self.event_at,
        )

        rows = build_mechanic_downtime_rows(self.selected_date)

        self.assertEqual([row['reason'].name for row in rows], ['Ремонт'])

    def test_management_risks_only_count_current_open_contracts(self):
        mechanical, _ = DowntimeReason.objects.update_or_create(
            name='Ремонт',
            defaults={'equipment_type': self.excavator_type, 'show_for_mechanic': True},
        )
        operational, _ = DowntimeReason.objects.update_or_create(
            name='Заправка',
            defaults={'equipment_type': self.excavator_type, 'show_for_mechanic': False},
        )
        DowntimeEvent.objects.create(
            equipment=self.excavator,
            reason=mechanical,
            started_at=self.event_at,
        )
        DowntimeEvent.objects.create(
            equipment=self.excavator,
            reason=operational,
            started_at=self.event_at,
        )
        Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            is_carryover=True,
            completed_at=None,
        )
        Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            is_carryover=True,
            completed_at=self.event_at,
        )
        request = RequestFactory().get(
            f'/reports/management/?date={self.selected_date:%Y-%m-%d}',
        )

        context = management_dashboard_context(request, self.access)

        self.assertEqual(context['active_trip_count'], 1)
        self.assertEqual(context['carryover_trip_count'], 1)
        self.assertEqual(context['daily_carryover_trip_count'], 1)
        self.assertEqual(context['open_mechanic_downtime_count'], 1)

    def test_dispatcher_report_with_fact_and_no_plan_is_ready_not_critical(self):
        Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            volume_m3=Decimal('198.00'),
            tonnage=Decimal('473.31'),
            completed_at=self.event_at,
        )
        request = RequestFactory().get(
            f'/dispatcher/reports/?date={self.selected_date:%Y-%m-%d}',
        )

        context = dispatcher_reports_context(request, self.access)
        mining_tile = next(tile for tile in context['report_tiles'] if tile['kind'] == 'mining')
        customer_tile = next(tile for tile in context['report_tiles'] if tile['kind'] == 'customer')

        self.assertEqual(mining_tile['status'], 'ok')
        self.assertEqual(mining_tile['status_label'], 'готов')
        self.assertEqual(mining_tile['readiness'], 'План не задан')
        self.assertEqual(customer_tile['status'], 'ok')


class ShiftAnalyticsReportTests(TestCase):
    def setUp(self):
        self.admin_role = Role.objects.create(code='admin', name='Администратор')
        self.admin = Employee.objects.create(full_name='Администратор')
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.admin,
            role=self.admin_role,
            access_code='100000',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        session = self.client.session
        session['employee_access_id'] = self.admin_access.id
        session.save()

        self.truck_type = EquipmentType.objects.create(name='Самосвал')
        self.excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck_model = EquipmentModel.objects.create(
            equipment_type=self.truck_type,
            name='БелАЗ тест',
            body_volume_m3=Decimal('38.00'),
        )
        self.excavator_model = EquipmentModel.objects.create(
            equipment_type=self.excavator_type,
            name='ЭКГ тест',
        )
        self.truck = Equipment.objects.create(equipment_type=self.truck_type, model=self.truck_model, garage_number='25')
        self.open_truck = Equipment.objects.create(equipment_type=self.truck_type, model=self.truck_model, garage_number='26')
        self.excavator = Equipment.objects.create(equipment_type=self.excavator_type, model=self.excavator_model, garage_number='4')
        self.rock = RockType.objects.create(name='Руда', density=Decimal('2.0000'))
        self.dump_point = DumpPoint.objects.create(name='ККД')
        TruckCapacityRule.objects.create(equipment_model=self.truck_model, rock_type=self.rock, volume_m3=Decimal('38.00'))

        self.driver = Employee.objects.create(full_name='Водитель')
        self.operator = Employee.objects.create(full_name='Машинист')
        self.date = timezone.localdate()
        opened_at = timezone.make_aware(
            datetime.combine(self.date, time(10, 0)),
            timezone.get_current_timezone(),
        )
        self.driver_shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type='day',
            equipment=self.truck,
            opened_at=opened_at,
        )
        self.operator_shift = EmployeeShift.objects.create(
            employee=self.operator,
            shift_type='day',
            equipment=self.excavator,
            opened_at=opened_at,
        )
        self.completed_trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            excavator_operator=self.operator,
            driver=self.driver,
            loading_shift=self.operator_shift,
            unloading_shift=self.driver_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            actual_dump_point=self.dump_point,
            volume_m3=Decimal('40.00'),
            tonnage=Decimal('80.00'),
            loading_horizon='75',
            loading_block='52',
            status=TripStatus.COMPLETED,
            completed_at=opened_at,
        )
        self.open_trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.open_truck,
            excavator_operator=self.operator,
            loading_shift=self.operator_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            assigned_dump_point=self.dump_point,
            loading_horizon='75',
            loading_block='53',
            status=TripStatus.LOADED_WAITING_UNLOAD,
        )
        Trip.objects.filter(pk__in=[self.completed_trip.pk, self.open_trip.pk]).update(created_at=opened_at)
        self.downtime_reason = DowntimeReason.objects.create(name='Тестовая зачистка забоя', show_for_excavator_operator=True)
        DowntimeEvent.objects.create(
            equipment=self.excavator,
            employee=self.operator,
            reason=self.downtime_reason,
            started_at=opened_at,
        )

    def test_shift_analytics_counts_loading_unloading_and_downtimes(self):
        analytics = build_shift_analytics(self.date, 'day')

        self.assertEqual(analytics['totals']['loaded_trip_count'], 2)
        self.assertEqual(analytics['totals']['unloaded_trip_count'], 1)
        self.assertEqual(analytics['totals']['open_trip_count'], 1)
        self.assertEqual(analytics['totals']['volume_m3'], Decimal('78.00'))
        self.assertEqual(analytics['totals']['tonnage'], Decimal('156.00'))
        self.assertEqual(analytics['totals']['downtime_count'], 1)
        self.assertEqual(analytics['excavator_rows'][0]['loaded_count'], 2)
        self.assertEqual(analytics['truck_rows'][0]['unloaded_count'], 1)
        self.assertEqual(analytics['employee_rows'][0]['label'], 'Машинист')
        self.assertEqual(analytics['rock_rows'][0]['label'], 'Руда')
        self.assertEqual(analytics['face_rows'][0]['label'], '75 / 52')

    def test_shift_analytics_uses_authoritative_shift_production_date(self):
        previous_opened_at = timezone.make_aware(
            datetime.combine(self.date - timedelta(days=1), time(20, 0)),
            timezone.get_current_timezone(),
        )
        long_operator_shift = EmployeeShift.objects.create(
            employee=self.operator,
            shift_type='day',
            equipment=self.excavator,
            opened_at=previous_opened_at,
            closed_at=timezone.now(),
        )
        long_driver_shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type='day',
            equipment=self.truck,
            opened_at=previous_opened_at,
            closed_at=timezone.now(),
        )
        Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            excavator_operator=self.operator,
            driver=self.driver,
            loading_shift=long_operator_shift,
            unloading_shift=long_driver_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            actual_dump_point=self.dump_point,
            loading_horizon='76',
            loading_block='54',
            status=TripStatus.COMPLETED,
            completed_at=timezone.now(),
        )

        analytics = build_shift_analytics(self.date, 'day')

        self.assertEqual(analytics['totals']['loaded_trip_count'], 2)
        self.assertEqual(analytics['totals']['unloaded_trip_count'], 1)
        self.assertEqual(analytics['totals']['volume_m3'], Decimal('78.00'))

    def test_shift_analytics_report_page_renders_numbers(self):
        response = self.client.get(reverse('shift_analytics_report'), {'date': self.date.strftime('%Y-%m-%d'), 'shift_type': 'day'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Сменная аналитика')
        self.assertContains(response, '78 м3')
        self.assertContains(response, 'Экскаваторы')
        self.assertContains(response, 'Самосвалы')
        self.assertContains(response, 'Сотрудники')
        self.assertContains(response, 'Тестовая зачистка забоя')
        self.assertContains(response, 'data-shift-analytics-refresh-root')
        self.assertContains(response, 'window.applyOperationalStateRefresh')
        self.assertContains(response, 'refreshShiftAnalyticsFromServer')

    def test_management_dynamics_counts_excavator_volume(self):
        dynamics = build_excavator_dynamics(self.date, self.date, 'day', [self.excavator.id])

        self.assertEqual(dynamics['total_volume'], Decimal('78.00'))
        self.assertEqual(dynamics['trip_count'], 2)
        self.assertEqual(dynamics['excavator_count'], 1)
        self.assertEqual(dynamics['bucket_rows'][0]['volume_display'], '78')
        self.assertIn('Экскаватор 4', dynamics['excavator_rows'][0]['label'])
        self.assertEqual(dynamics['best_excavator']['volume_display'], '78')
        self.assertTrue(dynamics['chart_series'])
        self.assertTrue(dynamics['analysis_signals'])

    def test_management_dynamics_applies_shift_filter_for_every_granularity(self):
        matrix_excavator = Equipment.objects.create(
            equipment_type=self.excavator_type,
            model=self.excavator_model,
            garage_number='МАТРИЦА-Э',
        )
        matrix_truck = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='МАТРИЦА-С',
        )
        day_at = timezone.make_aware(
            datetime.combine(self.date, time(10, 0)),
            timezone.get_current_timezone(),
        )
        night_at = timezone.make_aware(
            datetime.combine(self.date, time(20, 0)),
            timezone.get_current_timezone(),
        )
        day_shift = EmployeeShift.objects.create(
            employee=Employee.objects.create(full_name='Машинист матрицы день'),
            shift_type='day',
            equipment=matrix_excavator,
            opened_at=day_at,
            closed_at=night_at,
        )
        night_shift = EmployeeShift.objects.create(
            employee=Employee.objects.create(full_name='Машинист матрицы ночь'),
            shift_type='night',
            equipment=matrix_excavator,
            opened_at=night_at,
        )
        for marker, event_at, shift, volume in (
            ('day', day_at, day_shift, Decimal('11.00')),
            ('night', night_at, night_shift, Decimal('22.00')),
        ):
            trip = Trip.objects.create(
                excavator=matrix_excavator,
                truck=matrix_truck,
                loading_shift=shift,
                rock_type=self.rock,
                dump_point=self.dump_point,
                volume_m3=volume,
                status=TripStatus.COMPLETED,
                completed_at=event_at,
            )
            Trip.objects.filter(pk=trip.pk).update(created_at=event_at)

        for shift_type, expected_volume in (
            ('day', Decimal('11.00')),
            ('night', Decimal('22.00')),
        ):
            for granularity in ('hour', 'day', 'shift', 'month'):
                with self.subTest(shift_type=shift_type, granularity=granularity):
                    dynamics = build_excavator_dynamics(
                        self.date,
                        self.date,
                        granularity,
                        [matrix_excavator.id],
                        shift_type=shift_type,
                    )
                    self.assertEqual(dynamics['trip_count'], 1)
                    self.assertEqual(dynamics['total_volume'], expected_volume)
                    self.assertEqual(
                        sum(row['trip_count'] for row in dynamics['bucket_rows']),
                        1,
                    )
                    self.assertTrue(dynamics['chart_points'])
                    self.assertTrue(dynamics['chart_series'])

    def test_management_dynamics_keeps_authoritative_day_carryover_at_1900_boundary(self):
        matrix_excavator = Equipment.objects.create(
            equipment_type=self.excavator_type,
            model=self.excavator_model,
            garage_number='QA-P1-006-Э',
        )
        matrix_truck = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='QA-P1-006-С',
        )
        day_at = timezone.make_aware(
            datetime.combine(self.date, time(7, 0)),
            timezone.get_current_timezone(),
        )
        night_at = timezone.make_aware(
            datetime.combine(self.date, time(19, 0)),
            timezone.get_current_timezone(),
        )
        next_day_at = timezone.make_aware(
            datetime.combine(self.date + timedelta(days=1), time(7, 0)),
            timezone.get_current_timezone(),
        )
        day_loading_shift = EmployeeShift.objects.create(
            employee=Employee.objects.create(full_name='QA-P1-006 машинист день'),
            shift_type='day',
            equipment=matrix_excavator,
            opened_at=day_at,
            closed_at=night_at,
        )
        day_unloading_shift = EmployeeShift.objects.create(
            employee=Employee.objects.create(full_name='QA-P1-006 водитель день'),
            shift_type='day',
            equipment=matrix_truck,
            opened_at=day_at,
            closed_at=night_at,
        )
        night_unloading_shift = EmployeeShift.objects.create(
            employee=Employee.objects.create(full_name='QA-P1-006 водитель ночь'),
            shift_type='night',
            equipment=matrix_truck,
            opened_at=night_at,
            closed_at=next_day_at,
        )
        trip_rows = (
            (time(10, 0), day_unloading_shift),
            (time(18, 0), day_unloading_shift),
            (time(19, 2), night_unloading_shift),
        )
        for created_time, unloading_shift in trip_rows:
            created_at = timezone.make_aware(
                datetime.combine(self.date, created_time),
                timezone.get_current_timezone(),
            )
            trip = Trip.objects.create(
                excavator=matrix_excavator,
                truck=matrix_truck,
                loading_shift=day_loading_shift,
                unloading_shift=unloading_shift,
                rock_type=self.rock,
                dump_point=self.dump_point,
                volume_m3=Decimal('47.00'),
                status=TripStatus.COMPLETED,
                completed_at=created_at,
            )
            Trip.objects.filter(pk=trip.pk).update(created_at=created_at)

        legacy_at = timezone.make_aware(
            datetime.combine(self.date, time(19, 2)),
            timezone.get_current_timezone(),
        )
        legacy_trip = Trip.objects.create(
            excavator=matrix_excavator,
            truck=matrix_truck,
            rock_type=self.rock,
            dump_point=self.dump_point,
            volume_m3=Decimal('13.00'),
            status=TripStatus.COMPLETED,
            completed_at=legacy_at,
        )
        Trip.objects.filter(pk=legacy_trip.pk).update(created_at=legacy_at)

        for granularity in ('hour', 'day', 'shift', 'month'):
            with self.subTest(granularity=granularity):
                day_dynamics = build_excavator_dynamics(
                    self.date,
                    self.date,
                    granularity,
                    [matrix_excavator.id],
                    shift_type='day',
                )
                self.assertEqual(day_dynamics['trip_count'], 3)
                self.assertEqual(day_dynamics['total_volume'], Decimal('141.00'))
                night_dynamics = build_excavator_dynamics(
                    self.date,
                    self.date,
                    granularity,
                    [matrix_excavator.id],
                    shift_type='night',
                )
                self.assertEqual(night_dynamics['trip_count'], 1)
                self.assertEqual(night_dynamics['total_volume'], Decimal('13.00'))

        hourly_day = build_excavator_dynamics(
            self.date,
            self.date,
            'hour',
            [matrix_excavator.id],
            shift_type='day',
        )
        self.assertEqual(sum(row['trip_count'] for row in hourly_day['bucket_rows']), 3)
        self.assertEqual(sum(row['volume_m3'] for row in hourly_day['bucket_rows']), Decimal('141.00'))
        boundary_bucket = next(row for row in hourly_day['bucket_rows'] if row['label'].endswith('19:00'))
        self.assertEqual(boundary_bucket['trip_count'], 1)
        self.assertEqual(boundary_bucket['volume_m3'], Decimal('47.00'))
        self.assertEqual(hourly_day['chart_series'][0]['tooltip_points'][-1]['value'], '141')
        hourly_day_trips = build_excavator_dynamics(
            self.date,
            self.date,
            'hour',
            [matrix_excavator.id],
            shift_type='day',
            chart_mode='trips',
        )
        self.assertEqual(hourly_day_trips['chart_series'][0]['tooltip_points'][-1]['value'], '3')

        hourly_night = build_excavator_dynamics(
            self.date,
            self.date,
            'hour',
            [matrix_excavator.id],
            shift_type='night',
        )
        self.assertEqual(hourly_night['trip_count'], 1)
        self.assertEqual(hourly_night['total_volume'], Decimal('13.00'))

    def test_management_dynamics_keeps_authoritative_night_carryover_at_0700_boundary(self):
        matrix_excavator = Equipment.objects.create(
            equipment_type=self.excavator_type,
            model=self.excavator_model,
            garage_number='QA-P1-006-НОЧЬ-Э',
        )
        matrix_truck = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='QA-P1-006-НОЧЬ-С',
        )
        night_at = timezone.make_aware(
            datetime.combine(self.date, time(19, 0)),
            timezone.get_current_timezone(),
        )
        next_day_boundary = timezone.make_aware(
            datetime.combine(self.date + timedelta(days=1), time(7, 0)),
            timezone.get_current_timezone(),
        )
        night_loading_shift = EmployeeShift.objects.create(
            employee=Employee.objects.create(full_name='QA-P1-006 машинист ночь'),
            shift_type='night',
            equipment=matrix_excavator,
            opened_at=night_at,
            closed_at=next_day_boundary,
        )
        normal_at = timezone.make_aware(
            datetime.combine(self.date, time(20, 0)),
            timezone.get_current_timezone(),
        )
        normal_trip = Trip.objects.create(
            excavator=matrix_excavator,
            truck=matrix_truck,
            loading_shift=night_loading_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            volume_m3=Decimal('47.00'),
            status=TripStatus.COMPLETED,
            completed_at=normal_at,
        )
        Trip.objects.filter(pk=normal_trip.pk).update(created_at=normal_at)
        carryover_at = next_day_boundary + timedelta(minutes=2)
        trip = Trip.objects.create(
            excavator=matrix_excavator,
            truck=matrix_truck,
            loading_shift=night_loading_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            volume_m3=Decimal('47.00'),
            status=TripStatus.COMPLETED,
            completed_at=carryover_at,
        )
        Trip.objects.filter(pk=trip.pk).update(created_at=carryover_at)

        for granularity in ('hour', 'day', 'shift', 'month'):
            with self.subTest(granularity=granularity):
                dynamics = build_excavator_dynamics(
                    self.date,
                    self.date,
                    granularity,
                    [matrix_excavator.id],
                    shift_type='night',
                )
                self.assertEqual(dynamics['trip_count'], 2)
                self.assertEqual(dynamics['total_volume'], Decimal('94.00'))
                self.assertEqual(sum(row['trip_count'] for row in dynamics['bucket_rows']), 2)
                self.assertEqual(
                    sum(row['volume_m3'] for row in dynamics['bucket_rows']),
                    Decimal('94.00'),
                )
                self.assertEqual(dynamics['chart_series'][0]['tooltip_points'][-1]['value'], '94')
                trips_dynamics = build_excavator_dynamics(
                    self.date,
                    self.date,
                    granularity,
                    [matrix_excavator.id],
                    shift_type='night',
                    chart_mode='trips',
                )
                self.assertEqual(trips_dynamics['chart_series'][0]['tooltip_points'][-1]['value'], '2')
                day_dynamics = build_excavator_dynamics(
                    self.date,
                    self.date,
                    granularity,
                    [matrix_excavator.id],
                    shift_type='day',
                )
                self.assertEqual(day_dynamics['trip_count'], 0)

        hourly_dynamics = build_excavator_dynamics(
            self.date,
            self.date,
            'hour',
            [matrix_excavator.id],
            shift_type='night',
        )
        boundary_bucket = next(row for row in hourly_dynamics['bucket_rows'] if row['label'].endswith('07:00'))
        self.assertEqual(boundary_bucket['trip_count'], 1)
        self.assertEqual(boundary_bucket['volume_m3'], Decimal('47.00'))

    def test_management_dynamics_hour_range_keeps_start_and_end_dates(self):
        previous_date = self.date - timedelta(days=1)
        previous_created_at = timezone.make_aware(
            datetime.combine(previous_date, time(16, 0)),
            timezone.get_current_timezone(),
        )
        previous_trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            excavator_operator=self.operator,
            driver=self.driver,
            loading_shift=self.operator_shift,
            unloading_shift=self.driver_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            actual_dump_point=self.dump_point,
            volume_m3=Decimal('12.00'),
            tonnage=Decimal('24.00'),
            status=TripStatus.COMPLETED,
            completed_at=previous_created_at,
        )
        Trip.objects.filter(pk=previous_trip.pk).update(created_at=previous_created_at)

        dynamics = build_excavator_dynamics(previous_date, self.date, 'hour', [self.excavator.id], shift_type='day')

        self.assertEqual(dynamics['date_from'], previous_date)
        self.assertEqual(dynamics['date_to'], self.date)
        self.assertEqual(len(dynamics['bucket_rows']), 26)
        self.assertEqual(dynamics['total_volume'], Decimal('90.00'))
        self.assertEqual(dynamics['bucket_rows'][0]['label'], f'{previous_date:%d.%m} 07:00')
        self.assertEqual(dynamics['bucket_rows'][-1]['label'], f'{self.date:%d.%m} 19:00')

    def test_management_dynamics_chart_modes_use_loaded_events(self):
        event_at = timezone.make_aware(
            datetime.combine(self.date, time(11, 15)),
            timezone.get_current_timezone(),
        )
        action = TripClientAction.objects.create(
            action_type='truck_loaded',
            client_action_id='dyn-loaded-1',
            trip=self.completed_trip,
            actor=self.operator,
        )
        TripClientAction.objects.filter(pk=action.pk).update(created_at=event_at)

        dynamics = build_excavator_dynamics(
            self.date,
            self.date,
            'hour',
            [self.excavator.id],
            shift_type='day',
            chart_mode='trips',
        )

        self.assertEqual(dynamics['chart_mode'], 'trips')
        self.assertEqual(dynamics['chart_y_axis_title'], 'рейсы')
        self.assertTrue(dynamics['chart_series'])
        self.assertTrue(dynamics['chart_series'][0]['area_path'])
        self.assertIn('11:00', [tick['label'] for tick in dynamics['chart_x_axis_ticks']])

    def test_management_dynamics_loaded_event_does_not_move_report_bucket(self):
        created_at = timezone.make_aware(
            datetime.combine(self.date, time(10, 45)),
            timezone.get_current_timezone(),
        )
        loaded_at = timezone.make_aware(
            datetime.combine(self.date, time(11, 15)),
            timezone.get_current_timezone(),
        )
        other_excavator = Equipment.objects.create(
            equipment_type=self.excavator_type,
            model=self.excavator_model,
            garage_number='9',
        )
        other_operator_shift = EmployeeShift.objects.create(
            employee=self.operator,
            shift_type='day',
            equipment=other_excavator,
            opened_at=created_at,
            closed_at=loaded_at,
        )
        trip = Trip.objects.create(
            excavator=other_excavator,
            truck=self.truck,
            excavator_operator=self.operator,
            driver=self.driver,
            loading_shift=other_operator_shift,
            unloading_shift=self.driver_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            actual_dump_point=self.dump_point,
            volume_m3=Decimal('40.00'),
            tonnage=Decimal('80.00'),
            status=TripStatus.COMPLETED,
            completed_at=loaded_at,
        )
        Trip.objects.filter(pk=trip.pk).update(created_at=created_at)
        action = TripClientAction.objects.create(
            action_type='truck_loaded',
            client_action_id='dyn-loaded-bucket-check',
            trip=trip,
            actor=self.operator,
        )
        TripClientAction.objects.filter(pk=action.pk).update(created_at=loaded_at)

        dynamics = build_excavator_dynamics(
            self.date,
            self.date,
            'hour',
            [other_excavator.id],
            shift_type='day',
            chart_mode='trips',
        )

        rows_by_label = {row['label'][-5:]: row for row in dynamics['bucket_rows']}
        self.assertEqual(rows_by_label['10:00']['volume_m3'], Decimal('40.00'))
        self.assertEqual(rows_by_label['11:00']['volume_m3'], Decimal('0'))
        self.assertTrue(dynamics['chart_series'])

    def test_management_dynamics_page_renders_graph(self):
        response = self.client.get(
            reverse('management_dynamics'),
            {
                'date_from': self.date.strftime('%Y-%m-%d'),
                'date_to': self.date.strftime('%Y-%m-%d'),
                'granularity': 'day',
                'excavators': [str(self.excavator.id)],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Динамика экскаваторов')
        self.assertContains(response, '78 м3')
        self.assertContains(response, 'management-dynamics-excavator-chip')
        self.assertContains(response, 'data-management-dynamics-all')
        self.assertContains(response, 'data-management-dynamics-selector-toggle')
        self.assertContains(response, 'management-dynamics-selector-popover')
        self.assertContains(response, 'management-dynamics-table')
        self.assertContains(response, 'management-dynamics-signals')
        self.assertContains(response, 'name="excavators"')
        self.assertNotContains(response, 'select name="excavators" multiple')
        self.assertContains(response, 'name="chart_mode"')
        self.assertContains(response, 'data-management-dynamics-chart-mode')
        self.assertContains(response, 'management-dynamics-chart-mode')
        self.assertContains(response, 'data-management-dynamics-refresh-root')
        self.assertContains(response, 'refreshManagementDynamicsFromServer')
        self.assertContains(response, 'window.applyOperationalStateRefresh')

    def test_management_dashboard_context_includes_shift_analytics_flow(self):
        request = RequestFactory().get('/reports/management/', {'date': self.date.strftime('%Y-%m-%d')})

        context = management_dashboard_context(request, self.admin_access)

        self.assertEqual(context['shift_analytics_totals']['loaded_trip_count'], 2)
        self.assertEqual(context['shift_analytics_totals']['unloaded_trip_count'], 1)
        self.assertEqual(context['shift_analytics_totals']['open_trip_count'], 1)
        self.assertEqual(context['shift_analytics_totals']['volume_m3'], Decimal('78.00'))
        self.assertEqual(context['shift_analytics_shift_cards'][0]['totals']['loaded_trip_count'], 2)

    def test_management_dashboard_page_renders_shift_analytics_flow(self):
        response = self.client.get(reverse('management_dashboard'), {'date': self.date.strftime('%Y-%m-%d')})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Погрузка')
        self.assertContains(response, 'Выгрузка')
        self.assertContains(response, 'Поток смены')
