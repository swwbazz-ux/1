"""Пульт мастера: собственная смена, настоящее авторство и отдельные права."""

import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.urls import include, path
from django.utils import timezone

from assignments.models import AssignmentStatus, ExcavatorPlacement, HaulAssignment
from core.models import OperationalStateEvent
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import (
    DumpPoint, Equipment, EquipmentModel, EquipmentType, RockType, TruckCapacityRule,
)
from shifts.models import EmployeeShift, ShiftClientAction
from users.active_role import ACTIVE_ROLE_GENERATION_SESSION_KEY
from users.models import Employee, EmployeeAccess, Role

from . import views
from .dispatcher_guards import dispatcher_access_from_request
from .models import DispatcherActionLog, Trip, TripStatus
from .tablet_control import get_control_shift, is_tablet_master_request


urlpatterns = [
    path('mining-master/tablet/', lambda request: HttpResponse(), name='mining_master_tablet'),
    path('', include('config.urls')),
]


@override_settings(ROOT_URLCONF=__name__)
class TabletMasterControlTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.master_role = Role.objects.create(code='mining_master', name='Горный мастер')
        self.master = Employee.objects.create(full_name='Мастер планшета', status=Employee.Status.ACTIVE)
        self.access = EmployeeAccess.objects.create(
            employee=self.master, role=self.master_role,
            access_code='491827', status=EmployeeAccess.Status.ACTIVATED,
        )
        self.shift = EmployeeShift.objects.create(
            employee=self.master, workplace_code='mining_master', shift_type='day',
            opened_by=self.master, opened_at=timezone.now() - timedelta(minutes=5),
        )
        self.dispatcher = Employee.objects.create(full_name='Диспетчер компьютера', status=Employee.Status.ACTIVE)
        self.dispatcher_access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=Role.objects.create(code='dispatcher', name='Диспетчер'),
            access_code='491828', status=EmployeeAccess.Status.ACTIVATED,
        )
        self.dispatcher_shift = EmployeeShift.objects.create(
            employee=self.dispatcher, workplace_code='dispatcher', shift_type='day',
            opened_by=self.dispatcher, opened_at=timezone.now(),
        )
        truck_type = EquipmentType.objects.create(name='Самосвал')
        truck_model = EquipmentModel.objects.create(equipment_type=truck_type, name='Тестовая модель')
        self.truck = Equipment.objects.create(
            equipment_type=truck_type, model=truck_model, garage_number='731',
        )
        self.excavator = Equipment.objects.create(
            equipment_type=EquipmentType.objects.create(name='Экскаватор'), garage_number='732',
        )
        self.placement = ExcavatorPlacement.objects.create(
            excavator=self.excavator, zone=ExcavatorPlacement.Zone.ACTIVE,
            changed_by=self.master,
        )
        self.rock = RockType.objects.create(name='Руда', density='2.5', loosening_factor='1.5')
        self.dump = DumpPoint.objects.create(name='Отвал')
        TruckCapacityRule.objects.create(
            equipment_model=truck_model, rock_type=self.rock, volume_m3='80',
        )
        self.driver = Employee.objects.create(full_name='Водитель планшетного теста', status=Employee.Status.ACTIVE)
        self.driver_shift = EmployeeShift.objects.create(
            employee=self.driver, workplace_code='driver', equipment=self.truck,
            shift_type='day', opened_at=timezone.now() - timedelta(minutes=5),
        )

    def request(self, payload=None, *, json_body=False, marker=True, access=None):
        request = self.factory.post(
            '/mining-master/tablet/action/',
            data=json.dumps(payload or {}) if json_body else (payload or {}),
            **({'content_type': 'application/json'} if json_body else {}),
        )
        request.session = {'employee_access_id': (access or self.access).id}
        request._messages = FallbackStorage(request)
        request.mining_master_tablet_control = marker
        return request

    def assign_payload(self):
        return {
            'action': 'assign', 'truck_id': self.truck.id, 'excavator_id': self.excavator.id,
            'expected_assignment_state_id': 0, 'client_action_id': 'tablet-assign-1',
        }

    def create_trip(self):
        return Trip.objects.create(
            excavator=self.excavator, truck=self.truck, rock_type=self.rock,
            dump_point=self.dump, assigned_dump_point=self.dump, actual_dump_point=self.dump,
            status=TripStatus.LOADED_WAITING_UNLOAD, volume_m3='80', tonnage='200',
        )

    def test_own_shift_and_assignment_identity_do_not_depend_on_dispatcher_shift(self):
        self.dispatcher_shift.closed_at = timezone.now()
        self.dispatcher_shift.save(update_fields=['closed_at'])
        request = self.request(self.assign_payload(), json_body=True)
        self.assertEqual(get_control_shift(self.access, request), self.shift)
        first = views.dispatcher_assign_truck_view(request)
        repeated = views.dispatcher_assign_truck_view(self.request(self.assign_payload(), json_body=True))
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(repeated.status_code, 200, repeated.content)
        self.assertEqual(HaulAssignment.objects.get(truck=self.truck).assigned_by, self.master)
        self.assertEqual(DispatcherActionLog.objects.get().actor, self.master)
        action = ShiftClientAction.objects.get(client_action_id='tablet-assign-1')
        self.assertEqual(action.shift, self.shift)
        self.assertEqual(action.employee, self.master)
        self.assertEqual(EmployeeAccess.objects.filter(employee=self.master).count(), 1)

    def test_other_dispatcher_or_master_shift_cannot_authorize_tablet_mutation(self):
        self.shift.closed_at = timezone.now()
        self.shift.save(update_fields=['closed_at'])
        other = Employee.objects.create(full_name='Другой мастер', status=Employee.Status.ACTIVE)
        EmployeeShift.objects.create(
            employee=other, workplace_code='mining_master', shift_type='day', opened_at=timezone.now(),
        )
        request = self.request(self.assign_payload(), json_body=True)
        self.assertIsNone(get_control_shift(self.access, request))
        response = views.dispatcher_assign_truck_view(request)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(HaulAssignment.objects.exists())
        self.assertFalse(DispatcherActionLog.objects.exists())

    def test_legacy_dispatcher_urls_do_not_grant_master_permissions(self):
        request = self.request(self.assign_payload(), json_body=True, marker=False)
        self.assertIsNone(dispatcher_access_from_request(request))
        self.assertEqual(views.dispatcher_assign_truck_view(request).status_code, 403)
        self.assertFalse(HaulAssignment.objects.exists())

    def test_marker_cannot_turn_dispatcher_into_master(self):
        request = self.request(self.assign_payload(), json_body=True, access=self.dispatcher_access)
        self.assertFalse(is_tablet_master_request(request))
        self.assertEqual(views.dispatcher_assign_truck_view(request).status_code, 403)

    def test_revocation_and_new_generation_reject_even_previously_loaded_access(self):
        request = self.request()
        self.assertTrue(is_tablet_master_request(request, self.access))
        EmployeeAccess.objects.filter(pk=self.access.id).update(is_active=False)
        self.assertFalse(is_tablet_master_request(request, self.access))
        stamp = timezone.now()
        EmployeeAccess.objects.filter(pk=self.access.id).update(is_active=True, last_login_at=stamp)
        request.session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = (stamp - timedelta(seconds=1)).isoformat()
        self.assertFalse(is_tablet_master_request(request, self.access))
        self.assertIsNone(views.lock_dispatcher_mutation_access(request, self.access))

    def test_settings_use_master_shift_and_record_master_actor(self):
        response = views.dispatcher_equipment_detail_view(self.request({
            'state_version': views.get_operational_state_version(),
            'rock_type_id': self.rock.id,
            'destinations': [{'dump_point_id': self.dump.id, 'transport_distance_km': '3.75'}],
            'loading_horizon': '75', 'loading_block': '52',
        }, json_body=True), 'complex', self.excavator.id)
        self.assertEqual(response.status_code, 200, response.content)
        self.placement.refresh_from_db()
        self.assertEqual(self.placement.changed_by, self.master)
        self.assertEqual(self.placement.transport_distance_km, Decimal('3.75'))
        event = OperationalStateEvent.objects.get(payload__action='dispatcher_excavator_work_settings')
        self.assertEqual(event.payload['actor_id'], self.master.id)

    def test_move_to_garage_records_own_shift_and_master_actor(self):
        response = views.dispatcher_move_excavator_view(self.request({
            'excavator_id': self.excavator.id, 'zone': ExcavatorPlacement.Zone.INACTIVE,
            'expected_zone': ExcavatorPlacement.Zone.ACTIVE,
            'expected_assignment_states': {}, 'client_action_id': 'tablet-garage-1',
        }, json_body=True))
        self.assertEqual(response.status_code, 200, response.content)
        self.placement.refresh_from_db()
        self.assertEqual(self.placement.zone, ExcavatorPlacement.Zone.INACTIVE)
        self.assertEqual(self.placement.changed_by, self.master)
        self.assertEqual(ShiftClientAction.objects.get().shift, self.shift)
        self.assertEqual(DispatcherActionLog.objects.get().actor, self.master)

    def test_cancel_assignment_records_master_without_dispatcher_access(self):
        assignment = HaulAssignment.objects.create(
            truck=self.truck, excavator=self.excavator, assigned_by=self.master,
            status=AssignmentStatus.PENDING,
        )
        response = views.dispatcher_cancel_assignment_view(self.request({'reason': 'Переназначение'}), assignment.id)
        self.assertEqual(response.status_code, 302)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.CANCELLED)
        self.assertEqual(DispatcherActionLog.objects.get().actor, self.master)

    def test_trip_cancel_and_complete_keep_real_master_actor(self):
        trip = self.create_trip()
        cancelled = views.dispatcher_cancel_trip_view(self.request({'reason': 'Ошибка ввода'}), trip.id)
        self.assertEqual(cancelled.status_code, 302)
        self.assertEqual(cancelled['Location'], '/mining-master/tablet/')
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        trip = self.create_trip()
        completed = views.dispatcher_complete_trip_view(self.request({'reason': 'Подтверждено'}), trip.id)
        self.assertEqual(completed.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(set(DispatcherActionLog.objects.values_list('actor_id', flat=True)), {self.master.id})

    def test_manual_trip_keeps_driver_credit_and_master_audit(self):
        HaulAssignment.objects.create(
            truck=self.truck, excavator=self.excavator, assigned_by=self.master,
            status=AssignmentStatus.ACCEPTED,
        )
        response = views.dispatcher_manual_trip_view(self.request({
            'excavator_id': self.excavator.id, 'dump_point_id': self.dump.id,
            'rock_type_id': self.rock.id, 'trips_count': '1', 'reason': 'Пропущена отметка',
        }), self.truck.id)
        self.assertEqual(response.status_code, 302)
        trip = Trip.objects.get()
        self.assertEqual(trip.driver, self.driver)
        self.assertEqual(trip.unloading_shift, self.driver_shift)
        self.assertIn('горным мастером', trip.note)
        self.assertEqual(DispatcherActionLog.objects.get().actor, self.master)

    def test_service_close_requires_own_shift_and_records_master(self):
        response = views.dispatcher_service_close_shift_view(
            self.request({'reason': 'По согласованию'}), self.driver_shift.id,
        )
        self.assertEqual(response.status_code, 302)
        self.driver_shift.refresh_from_db()
        self.assertIsNotNone(self.driver_shift.closed_at)
        self.assertEqual(self.driver_shift.closed_by, self.master)
        self.assertEqual(DispatcherActionLog.objects.get().actor, self.master)
        self.shift.refresh_from_db()
        self.assertIsNone(self.shift.closed_at)

    def test_closed_own_shift_blocks_settings_and_trip_and_service_close(self):
        self.shift.closed_at = timezone.now()
        self.shift.save(update_fields=['closed_at'])
        trip = self.create_trip()
        views.dispatcher_cancel_trip_view(self.request({'reason': 'Не разрешено'}), trip.id)
        views.dispatcher_service_close_shift_view(self.request({'reason': 'Не разрешено'}), self.driver_shift.id)
        response = views.dispatcher_equipment_detail_view(self.request({}, json_body=True), 'complex', self.excavator.id)
        self.assertEqual(response.status_code, 409)
        trip.refresh_from_db()
        self.driver_shift.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertIsNone(self.driver_shift.closed_at)
        self.assertFalse(DispatcherActionLog.objects.exists())

    def test_downtime_close_keeps_master_actor(self):
        reason, _ = DowntimeReason.objects.get_or_create(name='Ремонт')
        event = DowntimeEvent.objects.create(
            equipment=self.truck, reason=reason, started_at=timezone.now() - timedelta(minutes=3),
        )
        response = views.dispatcher_close_downtime_view(self.request({
            'state_version': views.get_operational_state_version(),
        }, json_body=True), event.id)
        self.assertEqual(response.status_code, 200, response.content)
        event.refresh_from_db()
        self.assertIsNotNone(event.ended_at)
        audit = OperationalStateEvent.objects.get(payload__action='dispatcher_downtime_closed')
        self.assertEqual(audit.payload['actor_id'], self.master.id)
