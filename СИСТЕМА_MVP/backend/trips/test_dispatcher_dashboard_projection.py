"""Границы чистых представлений dashboard Диспетчерского пульта."""

import inspect
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from . import dispatcher_dashboard_projection
from . import views as trips_views


class DispatcherDashboardProjectionBoundaryTests(SimpleTestCase):
    def test_views_reexports_pure_complex_projection_helpers(self):
        self.assertIs(
            trips_views.dispatcher_complex_truck_rows,
            dispatcher_dashboard_projection.dispatcher_complex_truck_rows,
        )
        self.assertIs(
            trips_views.dispatcher_tons_from_label,
            dispatcher_dashboard_projection.dispatcher_tons_from_label,
        )
        self.assertIs(
            trips_views.dispatcher_complex_face_label,
            dispatcher_dashboard_projection.dispatcher_complex_face_label,
        )
        self.assertIs(
            trips_views.dispatcher_complex_location_parts,
            dispatcher_dashboard_projection.dispatcher_complex_location_parts,
        )
        self.assertIs(
            trips_views.dispatcher_plan_details,
            dispatcher_dashboard_projection.dispatcher_plan_details,
        )
        self.assertIs(
            trips_views.dispatcher_status_label,
            dispatcher_dashboard_projection.dispatcher_status_label,
        )
        self.assertIs(
            trips_views.dispatcher_garage_number_int,
            dispatcher_dashboard_projection.dispatcher_garage_number_int,
        )
        self.assertIs(
            trips_views.dispatcher_complex_label,
            dispatcher_dashboard_projection.dispatcher_complex_label,
        )
        self.assertIs(
            trips_views.dispatcher_complex_number_int,
            dispatcher_dashboard_projection.dispatcher_complex_number_int,
        )

    def test_views_keeps_compatibility_wrapper_for_report_formatters(self):
        source = inspect.getsource(trips_views.dispatcher_complex_shift_report)

        self.assertIn('_build_dispatcher_complex_shift_report(', source)
        self.assertIn('format_number=format_dispatcher_number', source)
        self.assertIn('chart_percent=dispatcher_chart_percent', source)
        self.assertNotIn('grouped_chart_rows', source)
        self.assertNotIn("status_key == 'red'", source)

    def test_projection_module_has_no_orm_or_cross_role_dependencies(self):
        source = inspect.getsource(dispatcher_dashboard_projection)

        self.assertNotIn('django.', source)
        self.assertNotIn('.objects', source)
        self.assertNotIn('Trip', source)
        self.assertNotIn('EmployeeShift', source)
        self.assertNotIn('ExcavatorPlacement', source)

    def test_dashboard_builder_uses_preserved_public_projection_names(self):
        source = inspect.getsource(trips_views.build_dispatcher_dashboard_context)

        self.assertIn('dispatcher_complex_shift_report(card)', source)
        self.assertIn('dispatcher_complex_location_parts(card)', source)
        self.assertIn('dispatcher_complex_face_label(card)', source)
        self.assertIn('dispatcher_plan_details(tile.get(', source)
        self.assertIn('dispatcher_complex_label(excavator)', source)
        self.assertIn('dispatcher_garage_number_int(excavator)', source)
        self.assertNotIn('def dispatcher_plan_details(', source)
        self.assertNotIn('def dispatcher_complex_label(', source)
        self.assertNotIn('def garage_number_int(', source)


class DispatcherDashboardProjectionBehaviorTests(SimpleTestCase):
    def setUp(self):
        self.card = {
            'status_key': 'yellow',
            'assigned': 2,
            'need': 3,
            'percent': 40,
            'current_horizon': 'Гор. 75',
            'current_block': 'Блок 52',
            'plan': {
                'value_display': '1 000',
                'unit': 'т',
                'fact_plan_label': '400 / 1 000 т',
                'group_name': 'Сменная группа',
            },
            'forecast_tons': '900',
            'truck_rows': [
                {
                    'state_key': 'current',
                    'truck': '101',
                    'rock': 'Скала',
                    'target': 'Отвал 1',
                    'value': '250 т',
                },
                {
                    'state_key': 'current',
                    'truck': '102',
                    'rock': 'Руда',
                    'target': 'Отвал 2',
                    'value': '150 т',
                },
                {
                    'state_key': 'removed',
                    'truck': '103',
                    'rock': 'Скала',
                    'target': 'Отвал 1',
                    'value': '50 т',
                },
            ],
        }

    def test_face_location_and_tonnage_formatting_are_preserved(self):
        self.assertEqual(
            dispatcher_dashboard_projection.dispatcher_complex_face_label(
                self.card,
            ),
            'Гор. 75 / Блок 52',
        )
        self.assertEqual(
            dispatcher_dashboard_projection.dispatcher_complex_location_parts(
                {},
            ),
            ('Гор. -', 'Блок -'),
        )
        self.assertEqual(
            dispatcher_dashboard_projection.dispatcher_complex_face_label({}),
            'Забой не указан',
        )
        self.assertEqual(
            dispatcher_dashboard_projection.dispatcher_tons_from_label(
                '1 234 т',
            ),
            Decimal('1234'),
        )

    def test_plan_detail_rows_and_status_label_are_preserved(self):
        plan = {
            'status_label': 'План назначен',
            'fact_plan_label': '400 / 1 000 т',
            'has_plan': True,
            'percent_label': '40%',
            'group_name': 'Сменная группа',
        }

        self.assertEqual(
            dispatcher_dashboard_projection.dispatcher_plan_details(plan),
            [
                {'label': 'Статус плана', 'value': 'План назначен'},
                {'label': 'Выполнение плана', 'value': '40%'},
                {'label': 'Факт / план', 'value': '400 / 1 000 т'},
                {'label': 'Группа плана', 'value': 'Сменная группа'},
            ],
        )
        self.assertEqual(
            dispatcher_dashboard_projection.dispatcher_plan_details(None),
            [],
        )
        self.assertEqual(
            dispatcher_dashboard_projection.dispatcher_status_label(
                'green',
                'Работает',
            ),
            'Работает',
        )
        self.assertEqual(
            dispatcher_dashboard_projection.dispatcher_status_label('gray'),
            '',
        )

    def test_complex_labels_and_sort_numbers_are_preserved(self):
        ordinary = SimpleNamespace(id=10, garage_number='ЭКГ-005')
        branded = SimpleNamespace(id=11, garage_number='ТВИ 4')
        unnamed = SimpleNamespace(id=12, garage_number='')

        self.assertEqual(
            dispatcher_dashboard_projection.dispatcher_complex_label(ordinary),
            'K-5',
        )
        self.assertEqual(
            dispatcher_dashboard_projection.dispatcher_complex_label(branded),
            'K-ТВИ-4',
        )
        self.assertEqual(
            dispatcher_dashboard_projection.dispatcher_complex_label(unnamed),
            'K-ID-12',
        )
        self.assertEqual(
            dispatcher_dashboard_projection.dispatcher_garage_number_int(
                branded,
            ),
            4,
        )
        self.assertEqual(
            dispatcher_dashboard_projection.dispatcher_garage_number_int(
                unnamed,
            ),
            9999,
        )
        self.assertEqual(
            dispatcher_dashboard_projection.dispatcher_complex_number_int(
                {'id': 'K-ТВИ-4'},
            ),
            4,
        )
        self.assertEqual(
            dispatcher_dashboard_projection.dispatcher_complex_number_int(
                {'id': 'K-БЕЗ-НОМЕРА'},
            ),
            9999,
        )

    def test_complex_report_preserves_metrics_charts_and_input(self):
        original = deepcopy(self.card)

        report = trips_views.dispatcher_complex_shift_report(self.card)

        self.assertEqual(self.card, original)
        self.assertEqual(report['problem'], 'ожидает действия')
        self.assertEqual(report['current_trucks'], ['101', '102'])
        self.assertEqual(report['removed_trucks'], ['103'])
        self.assertEqual(report['metrics'], [
            {'label': 'План', 'value': '1 000 т'},
            {'label': 'Факт', 'value': '400 / 1 000 т'},
            {'label': 'Самосвалы', 'value': '2 / 3'},
            {'label': 'Работали', 'value': '3'},
            {'label': 'Выведены', 'value': '1'},
        ])
        charts = {chart['title']: chart for chart in report['charts']}
        self.assertEqual(charts['План / факт']['rows'][0], {
            'label': 'Факт / план',
            'meta': 'Сменная группа',
            'value': '400 / 1 000 т',
            'percent': 40,
            'accent': 'yellow',
        })
        self.assertEqual(charts['Порода']['rows'], [
            {
                'label': 'Скала',
                'meta': 'Отвал 1',
                'value': '300 т',
                'percent': 100,
                'accent': 'green',
            },
            {
                'label': 'Руда',
                'meta': 'Отвал 2',
                'value': '150 т',
                'percent': 50,
                'accent': 'blue',
            },
        ])
        self.assertEqual(charts['Баланс']['rows'][0]['percent'], 66)
        self.assertEqual(charts['Баланс']['rows'][2], {
            'label': 'Баланс',
            'meta': 'добавить транспорт',
            'value': '-1',
            'percent': 33,
            'accent': 'red',
        })

    def test_views_wrapper_matches_projection_module(self):
        expected = (
            dispatcher_dashboard_projection.dispatcher_complex_shift_report(
                self.card,
                format_number=trips_views.format_dispatcher_number,
                chart_percent=trips_views.dispatcher_chart_percent,
            )
        )

        self.assertEqual(
            trips_views.dispatcher_complex_shift_report(self.card),
            expected,
        )
