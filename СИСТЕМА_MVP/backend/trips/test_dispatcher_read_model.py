import inspect
from unittest.mock import Mock

from django.test import RequestFactory, SimpleTestCase, TestCase

from users.models import Employee, EmployeeAccess, Role

from . import dispatcher_read_model
from . import views as trips_views


class DispatcherReadModelBoundaryTests(SimpleTestCase):
    def test_views_reexports_read_model_builder(self):
        self.assertIs(
            trips_views.build_dispatcher_control_read_model,
            dispatcher_read_model.build_dispatcher_control_read_model,
        )

    def test_dispatcher_controller_no_longer_owns_read_queries(self):
        source = inspect.getsource(trips_views.dispatcher_control_view)

        self.assertIn('build_dispatcher_control_read_model(', source)
        self.assertNotIn('Trip.objects', source)
        self.assertNotIn('HaulAssignment.objects', source)
        self.assertNotIn('EmployeeShift.objects', source)


class DispatcherReadModelTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.role = Role.objects.create(
            code='dispatcher',
            name='Горный диспетчер',
        )
        self.employee = Employee.objects.create(
            full_name='Диспетчер read-model',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.role,
            access_code='114411',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    def build_read_model(self, query='', *, context_overrides=None, equipment_detail=None):
        request = self.factory.get(f'/dispatcher/control/{query}')
        request.session = {'employee_access_id': self.access.id}
        dashboard_builder = Mock(return_value={'equipment_cards': {}})

        read_model = dispatcher_read_model.build_dispatcher_control_read_model(
            request,
            self.access,
            dashboard_builder=dashboard_builder,
            equipment_shift_is_current=Mock(return_value=True),
            dispatcher_header_override={'active_shift': None},
            context_overrides=context_overrides,
            equipment_detail=equipment_detail,
        )
        return read_model, dashboard_builder

    def test_filters_and_card_scope_are_preserved(self):
        read_model, dashboard_builder = self.build_read_model(
            '?truck=17&excavator=23&show_active_trips=0'
            '&show_pending_assignments=1&show_accepted_assignments=0',
            equipment_detail={'card_key': 'complex-equipment-23'},
        )

        self.assertEqual(
            read_model.filters,
            {
                'truck': '17',
                'excavator': '23',
                'show_active_trips': False,
                'show_pending_assignments': True,
                'show_accepted_assignments': False,
            },
        )
        self.assertEqual(
            read_model.dispatcher_filter_items,
            [
                ('truck', '17'),
                ('excavator', '23'),
                ('show_active_trips', '0'),
                ('show_pending_assignments', '1'),
                ('show_accepted_assignments', '0'),
            ],
        )
        self.assertEqual(
            dashboard_builder.call_args.kwargs['equipment_card_ids'],
            {'complex-equipment-23'},
        )

    def test_mining_master_keeps_full_equipment_card_map(self):
        reporting_period = {'starts_at': '2026-09-25T07:00:00+10:00'}
        _, dashboard_builder = self.build_read_model(
            context_overrides={
                'mining_master_mobile_enabled': True,
                'mining_master_reporting_period': reporting_period,
            },
        )

        kwargs = dashboard_builder.call_args.kwargs
        self.assertIsNone(kwargs['equipment_card_ids'])
        self.assertEqual(kwargs['reporting_period'], reporting_period)
