from datetime import datetime, time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase
from django.utils import timezone

from references.models import DumpPoint, Equipment, EquipmentType, RockType
from shifts.models import EmployeeShift
from trips.models import DispatcherActionLog, Trip, TripStatus
from users.active_role import activate_role_session
from users.models import Employee, EmployeeAccess, Role

from .models import ReportTemplate
from .views import (
    customer_daily_report_view,
    dispatcher_reports_view,
    dispatcher_shift_report_view,
    downtime_report_view,
    get_report_access,
    get_reports_access,
    report_template_builder_view,
    reports_mutation_role_barrier,
    require_dispatcher_report_access,
    update_shift_report_trip,
    volume_report_view,
)


class TabletMasterReportAccessTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.master_role = Role.objects.create(code='mining_master', name='Горный мастер')
        self.dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.master = self.create_access('Мастер отчётов', self.master_role, '746382')
        self.dispatcher = self.create_access('Диспетчер отчётов', self.dispatcher_role, '829475')

    def create_access(self, name, role, code):
        employee = Employee.objects.create(full_name=name, status=Employee.Status.ACTIVE)
        return EmployeeAccess.objects.create(
            employee=employee, role=role, access_code=code,
            status=EmployeeAccess.Status.ACTIVATED, is_active=True,
        )

    def request(self, access=None, *, tablet=True, data=None):
        request = self.factory.post('/mining-master/tablet/reports/templates/', data or {})
        request.session = {}
        request._messages = FallbackStorage(request)
        if tablet:
            request.mining_master_tablet_control = True
        if access:
            activate_role_session(request, access)
        return request

    def open_shift(self, access, workplace):
        return EmployeeShift.objects.create(
            employee=access.employee, opened_by=access.employee,
            workplace_code=workplace, shift_type='day', opened_at=timezone.now(),
        )

    def template_data(self):
        return {'name': 'Планшетный отчёт', 'columns': ['volume_m3'], 'is_active': 'on'}

    def test_master_can_read_reports_without_open_shift_only_in_tablet_scope(self):
        request = self.request(self.master)
        access, response = require_dispatcher_report_access(request)
        self.assertIsNone(response)
        self.assertEqual(access.pk, self.master.pk)
        self.assertEqual(get_reports_access(request, {'dispatcher', 'admin'}).pk, self.master.pk)
        request.mining_master_tablet_control = False
        access, response = require_dispatcher_report_access(request)
        self.assertIsNone(access)
        self.assertEqual(response.status_code, 302)

    def test_tablet_scope_never_grants_non_dispatcher_report_permissions(self):
        self.assertIsNone(get_reports_access(self.request(self.master), {'admin'}))

    def test_dispatcher_cannot_enter_master_alias(self):
        self.assertIsNone(get_report_access(self.request(self.dispatcher)))

    def test_revoked_access_and_stale_generation_are_rejected_for_reads(self):
        request = self.request(self.master)
        self.request(self.master)  # A later login invalidates the old session generation.
        self.assertIsNone(get_report_access(request))
        request = self.request(self.master)
        EmployeeAccess.objects.filter(pk=self.master.pk).update(is_active=False)
        self.assertIsNone(get_report_access(request))

    def test_closed_master_shift_cannot_write_report_template(self):
        request = self.request(self.master, data=self.template_data())
        response = report_template_builder_view(request)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ReportTemplate.objects.exists())

    def test_dispatcher_shift_does_not_unlock_master_report_writes(self):
        self.open_shift(self.dispatcher, 'dispatcher')
        response = report_template_builder_view(self.request(self.master, data=self.template_data()))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ReportTemplate.objects.exists())

    def test_another_master_shift_does_not_unlock_report_writes(self):
        other = self.create_access('Другой мастер', self.master_role, '723958')
        self.open_shift(other, 'mining_master')
        response = report_template_builder_view(self.request(self.master, data=self.template_data()))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ReportTemplate.objects.exists())

    def test_own_master_shift_saves_report_with_real_master_author(self):
        self.open_shift(self.master, 'mining_master')
        response = report_template_builder_view(self.request(self.master, data=self.template_data()))
        self.assertEqual(response.status_code, 302)
        template = ReportTemplate.objects.get()
        self.assertEqual(template.created_by_id, self.master.employee_id)
        self.assertEqual(template.updated_by_id, self.master.employee_id)

    def test_existing_dispatcher_template_write_policy_is_unchanged(self):
        response = report_template_builder_view(
            self.request(self.dispatcher, tablet=False, data=self.template_data())
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ReportTemplate.objects.get().created_by_id, self.dispatcher.employee_id)

    def test_mutation_barrier_requests_lock_on_own_control_shift(self):
        request = self.request(self.master)
        with patch('reports.views.get_control_shift', return_value=object()) as getter:
            access = get_report_access(request)
            self.assertIsNone(reports_mutation_role_barrier(request, access))
        getter.assert_called_once_with(access, request=request, for_update=True)

    def test_mutation_barrier_rejects_generation_changed_after_access_lookup(self):
        request = self.request(self.master)
        access = get_report_access(request)
        self.open_shift(self.master, 'mining_master')
        self.request(self.master)
        self.assertEqual(reports_mutation_role_barrier(request, access).status_code, 403)

    def completed_trip(self):
        day = timezone.localdate()
        completed_at = timezone.make_aware(datetime.combine(day, time(10, 0)))
        excavator = Equipment.objects.create(
            equipment_type=EquipmentType.objects.create(name='Экскаватор'), garage_number='Э-91',
        )
        truck = Equipment.objects.create(
            equipment_type=EquipmentType.objects.create(name='Самосвал'), garage_number='91',
        )
        return Trip.objects.create(
            excavator=excavator, truck=truck,
            rock_type=RockType.objects.create(name='Порода', density='2.6000'),
            dump_point=DumpPoint.objects.create(name='Отвал'),
            status=TripStatus.COMPLETED, completed_at=completed_at, volume_m3='10.00',
        ), day

    def correction_data(self, trip):
        return {
            'trip_id': trip.pk, 'correction_reason': 'Проверка замера',
            'volume_m3': '12.50', 'transport_distance_km': '2.10',
        }

    def test_report_correction_requires_own_shift_and_keeps_master_actor(self):
        trip, day = self.completed_trip()
        request = self.request(self.master, data=self.correction_data(trip))
        access = get_report_access(request)
        denied = update_shift_report_trip(request, access, day, 'day', 'trucks')
        self.assertEqual(denied.status_code, 403)
        trip.refresh_from_db()
        self.assertEqual(trip.volume_m3, Decimal('10.00'))
        self.assertFalse(DispatcherActionLog.objects.exists())
        self.open_shift(self.master, 'mining_master')
        response = update_shift_report_trip(request, access, day, 'day', 'trucks')
        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.volume_m3, Decimal('12.50'))
        log = DispatcherActionLog.objects.get(action_type='report_source_correction')
        self.assertEqual(log.actor_id, self.master.employee_id)

    def test_report_pages_keep_shared_workplace_shell_for_master(self):
        request = self.request(self.master)
        request.method = 'GET'
        # The tablet header itself is owned/tested by assignments.tablet_views.
        # Render real report templates to check role branches and report content.
        with patch('reports.views.build_report_header_context', return_value={
            'active_dispatcher': self.master.employee,
            'active_dispatcher_name': 'Мастер О.',
            'active_dispatcher_short_name': 'Мастер О.',
            'active_role_label': 'горный мастер',
        }):
            for view in (
                dispatcher_reports_view, dispatcher_shift_report_view,
                customer_daily_report_view, volume_report_view,
                downtime_report_view, report_template_builder_view,
            ):
                with self.subTest(view=view.__name__):
                    response = view(request)
                    self.assertEqual(response.status_code, 200)
                    html = response.content.decode()
                    self.assertTrue('dispatcher-unified-topbar' in html, view.__name__)
                    self.assertTrue('Мастер О.' in html, view.__name__)
