from datetime import datetime, time, timedelta
from decimal import Decimal

from django.urls import reverse
from django.test import RequestFactory, TestCase
from django.utils import timezone

from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import DumpPoint, Equipment, EquipmentModel, EquipmentType, RockType, TruckCapacityRule
from shifts.models import EmployeeShift, ShiftPlan
from trips.models import Trip, TripClientAction, TripStatus
from users.models import Employee, EmployeeAccess, Role

from .shift_analytics import build_excavator_dynamics, build_shift_analytics
from .views import management_dashboard_context


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

    def test_shift_analytics_uses_trip_event_date_not_shift_open_date(self):
        previous_opened_at = timezone.make_aware(
            datetime.combine(self.date - timedelta(days=1), time(20, 0)),
            timezone.get_current_timezone(),
        )
        long_operator_shift = EmployeeShift.objects.create(
            employee=self.operator,
            shift_type='day',
            equipment=self.excavator,
            opened_at=previous_opened_at,
        )
        long_driver_shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type='day',
            equipment=self.truck,
            opened_at=previous_opened_at,
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

        self.assertEqual(analytics['totals']['loaded_trip_count'], 3)
        self.assertEqual(analytics['totals']['unloaded_trip_count'], 2)
        self.assertEqual(analytics['totals']['volume_m3'], Decimal('116.00'))

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
