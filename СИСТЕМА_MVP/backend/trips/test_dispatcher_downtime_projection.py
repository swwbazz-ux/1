"""Границы read-model простоев Диспетчерского пульта."""

import inspect
from datetime import timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import Equipment, EquipmentType
from shifts.models import EmployeeShift
from users.models import Employee

from . import dispatcher_downtime_projection
from . import views as trips_views


class DispatcherDowntimeProjectionBoundaryTests(SimpleTestCase):
    def test_views_reexports_downtime_projection_helpers(self):
        helper_names = (
            'normalize_status_color_group',
            'downtime_reason_color_group',
            'dispatcher_alert_status_for_color_group',
            'dispatcher_alert_status_for_downtime',
            'format_duration_label',
            'format_dispatcher_datetime',
            'dispatcher_downtime_card_payload',
            'format_dispatcher_downtime_duration',
            'dispatcher_downtime_count_label',
            'dispatcher_shift_downtime_rows',
            'dispatcher_downtime_row_meta',
            'dispatcher_downtime_report_extras',
            'dispatcher_report_with_downtimes',
        )

        for helper_name in helper_names:
            with self.subTest(helper=helper_name):
                self.assertIs(
                    getattr(trips_views, helper_name),
                    getattr(dispatcher_downtime_projection, helper_name),
                )

    def test_projection_module_does_not_import_monolithic_views(self):
        source = inspect.getsource(dispatcher_downtime_projection)

        self.assertNotIn('from .views', source)
        self.assertNotIn('import trips.views', source)


class DispatcherDowntimeProjectionFormattingTests(SimpleTestCase):
    def test_status_duration_and_count_labels_are_preserved(self):
        self.assertEqual(
            dispatcher_downtime_projection.normalize_status_color_group('violet'),
            'yellow',
        )
        self.assertEqual(
            dispatcher_downtime_projection.dispatcher_alert_status_for_color_group(
                'red'
            ),
            'danger',
        )
        self.assertEqual(
            dispatcher_downtime_projection.dispatcher_alert_status_for_color_group(
                'orange'
            ),
            'warning',
        )
        self.assertEqual(
            dispatcher_downtime_projection.dispatcher_alert_status_for_color_group(
                'blue'
            ),
            'info',
        )
        self.assertEqual(
            dispatcher_downtime_projection.dispatcher_alert_status_for_color_group(
                'green'
            ),
            'ok',
        )
        self.assertEqual(
            dispatcher_downtime_projection.format_duration_label(3723),
            '01:02:03',
        )
        self.assertEqual(
            dispatcher_downtime_projection.format_dispatcher_downtime_duration(59),
            '59 с',
        )
        self.assertEqual(
            dispatcher_downtime_projection.format_dispatcher_downtime_duration(330),
            '5 мин 30 с',
        )
        self.assertEqual(
            dispatcher_downtime_projection.format_dispatcher_downtime_duration(3900),
            '1 ч 5 мин',
        )
        for count, expected in (
            (1, '1 событие'),
            (2, '2 события'),
            (5, '5 событий'),
            (11, '11 событий'),
            (21, '21 событие'),
        ):
            with self.subTest(count=count):
                self.assertEqual(
                    dispatcher_downtime_projection.dispatcher_downtime_count_label(
                        count
                    ),
                    expected,
                )

    def test_active_card_payload_keeps_timer_contract(self):
        calculated_at = timezone.now().replace(microsecond=0)
        started_at = calculated_at - timedelta(minutes=7, seconds=5)
        event = SimpleNamespace(
            id=14,
            equipment_id=22,
            started_at=started_at,
            reason=SimpleNamespace(button_label='Ремонт'),
        )

        payload = (
            dispatcher_downtime_projection.dispatcher_downtime_card_payload(
                event,
                calculated_at=calculated_at,
            )
        )

        self.assertEqual(payload['event_id'], 14)
        self.assertEqual(payload['equipment_id'], 22)
        self.assertEqual(payload['reason'], 'Ремонт')
        self.assertEqual(payload['started_at'], started_at.isoformat())
        self.assertEqual(payload['elapsed_seconds'], 425)
        self.assertEqual(payload['elapsed_label'], '00:07:05')
        self.assertEqual(
            payload['started_at_label'],
            dispatcher_downtime_projection.format_dispatcher_datetime(started_at),
        )
        self.assertEqual(
            dispatcher_downtime_projection.dispatcher_downtime_card_payload(None),
            {'active': False},
        )


class DispatcherDowntimeProjectionReadModelTests(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.employee = Employee.objects.create(full_name='Машинист простоя')
        self.equipment_type = EquipmentType.objects.create(name='Тестовая техника')
        self.equipment = Equipment.objects.create(
            equipment_type=self.equipment_type,
            garage_number='Д-1',
        )
        self.shift = EmployeeShift.objects.create(
            employee=self.employee,
            equipment=self.equipment,
            shift_type='day',
            workplace_code='truck_driver',
            opened_at=self.now - timedelta(hours=1),
            opened_by=self.employee,
        )
        self.repair_reason = DowntimeReason.objects.create(
            name='Критический ремонт проекции',
            short_label='Ремонт',
            equipment_type=self.equipment_type,
            is_critical=True,
        )
        self.waiting_reason = DowntimeReason.objects.create(
            name='Ожидание проекции',
            short_label='Ожидание',
            equipment_type=self.equipment_type,
        )
        DowntimeEvent.objects.create(
            equipment=self.equipment,
            employee=self.employee,
            subject_employee=self.employee,
            recorded_by=self.employee,
            reason=self.repair_reason,
            started_at=self.now - timedelta(hours=2),
            ended_at=self.now - timedelta(minutes=30),
        )
        DowntimeEvent.objects.create(
            equipment=self.equipment,
            employee=self.employee,
            subject_employee=self.employee,
            recorded_by=self.employee,
            reason=self.waiting_reason,
            started_at=self.now - timedelta(minutes=15),
        )

    def test_shift_rows_keep_handoff_timer_sorting_and_accents(self):
        rows = dispatcher_downtime_projection.dispatcher_shift_downtime_rows(
            self.equipment,
            self.shift,
            now=self.now,
        )

        self.assertEqual([row['label'] for row in rows], ['Ремонт', 'Ожидание'])
        self.assertEqual([row['seconds'] for row in rows], [1800, 900])
        self.assertTrue(rows[0]['is_inherited'])
        self.assertFalse(rows[0]['is_open'])
        self.assertEqual(rows[0]['accent'], 'red')
        self.assertFalse(rows[1]['is_inherited'])
        self.assertTrue(rows[1]['is_open'])
        self.assertEqual(rows[1]['accent'], 'yellow')

    def test_report_extras_and_merge_keep_existing_card_contract(self):
        metrics, charts = (
            dispatcher_downtime_projection.dispatcher_downtime_report_extras(
                self.equipment,
                self.shift,
                now=self.now,
            )
        )

        self.assertEqual(metrics, [
            {'label': 'Простои', 'value': '45 мин'},
            {'label': 'Доля смены', 'value': '75%'},
        ])
        self.assertEqual(len(charts), 1)
        self.assertEqual(charts[0]['title'], 'Простои смены')
        self.assertEqual(
            charts[0]['summary'],
            'Всего простоев 45 мин · 75% смены · 2 события',
        )
        self.assertIn('передан со смены', charts[0]['rows'][0]['meta'])
        self.assertEqual(
            charts[0]['rows'][1]['meta'],
            '1 событие · идёт сейчас',
        )

        original = {
            'metrics': [{'label': 'План', 'value': '100 т'}],
            'charts': [],
        }
        merged = dispatcher_downtime_projection.dispatcher_report_with_downtimes(
            original,
            self.equipment,
            self.shift,
            now=self.now,
        )

        self.assertEqual(original['metrics'], [{'label': 'План', 'value': '100 т'}])
        self.assertEqual(merged['metrics'][0], original['metrics'][0])
        self.assertEqual(merged['metrics'][1:], metrics)
        self.assertEqual(merged['charts'], charts)
